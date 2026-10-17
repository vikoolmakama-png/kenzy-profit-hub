import re
import shutil
import py_compile
from pathlib import Path

path = Path("app.py")
source = path.read_text()
original = source

backup = Path("app.py.before-referral-patch")
if not backup.exists():
    shutil.copy2(path, backup)


def replace_section(pattern, replacement, label):
    global source
    source, count = re.subn(
        pattern, lambda _: replacement, source, count=1, flags=re.S
    )
    if count != 1:
        raise RuntimeError(
            f"Could not safely find the {label} section. "
            "No changes have been saved."
        )


# 1. Add referral and bonus database tables and migrate existing users.
replace_section(
    r"def init_db\(\):.*?(?=\n@app\.route\(\"/\"\))",
    '''def init_db():
    conn = db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                phone TEXT NOT NULL UNIQUE,
                referral_code TEXT,
                password TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                referred_by INTEGER
            )
        """)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "referred_by" not in columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN referred_by INTEGER"
            )

        # Give every existing account a consistent, unique referral code.
        conn.execute("""
            UPDATE users
            SET referral_code = 'KPH' || printf('%05d', id)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS welcome_bonuses (
                user_id INTEGER PRIMARY KEY,
                transaction_id INTEGER NOT NULL UNIQUE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_rewards (
                plan_transaction_id INTEGER PRIMARY KEY,
                inviter_user_id INTEGER NOT NULL,
                referred_user_id INTEGER NOT NULL,
                reward_amount REAL NOT NULL,
                transaction_id INTEGER NOT NULL UNIQUE
            )
        """)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


''',
    "database initialization",
)


# 2. Register users, save their inviter, and credit the welcome bonus.
replace_section(
    r'@app\.route\("/register", methods=\["GET", "POST"\]\)\s*'
    r'def register\(\):.*?(?=\n@app\.route\("/login")',
    '''@app.route("/register", methods=["GET", "POST"])
def register():
    # Save the inviter code when a visitor opens a referral link.
    if request.method == "GET":
        link_code = request.args.get("ref", "").strip()
        if link_code:
            session["pending_referral_code"] = link_code

        return render_template("register.html")

    full_name = request.form.get("full_name", "").strip()
    username = request.form.get("username", "").strip()
    phone = request.form.get("phone", "").strip()
    referral_code = (
        request.form.get("referral_code", "").strip()
        or session.get("pending_referral_code", "").strip()
    )
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not all([full_name, username, phone, password, confirm_password]):
        flash("Please fill in all required fields.", "error")
        return redirect(url_for("register"))

    if len(username) < 3:
        flash("Username must contain at least 3 characters.", "error")
        return redirect(url_for("register"))

    if not re.fullmatch(r"[A-Za-z0-9_]+", username):
        flash("Username can contain letters, numbers and underscores only.", "error")
        return redirect(url_for("register"))

    if not re.fullmatch(r"\\+?[0-9]{7,15}", phone):
        flash("Enter a valid phone number.", "error")
        return redirect(url_for("register"))

    if len(password) < 8:
        flash("Password must contain at least 8 characters.", "error")
        return redirect(url_for("register"))

    if password != confirm_password:
        flash("Passwords do not match.", "error")
        return redirect(url_for("register"))

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")

        inviter = None
        if referral_code:
            inviter = conn.execute(
                """SELECT id FROM users
                   WHERE upper(referral_code) = upper(?)""",
                (referral_code,)
            ).fetchone()

            if not inviter:
                conn.rollback()
                flash("That referral code is invalid.", "error")
                return redirect(url_for("register"))

        inviter_id = inviter["id"] if inviter else None

        cursor = conn.execute(
            """INSERT INTO users
               (full_name, username, phone, referral_code,
                password, referred_by)
               VALUES (?, ?, ?, '', ?, ?)""",
            (
                full_name,
                username,
                phone,
                generate_password_hash(password),
                inviter_id,
            )
        )

        new_user_id = cursor.lastrowid
        own_code = f"KPH{new_user_id:05d}"

        conn.execute(
            "UPDATE users SET referral_code = ? WHERE id = ?",
            (own_code, new_user_id)
        )

        # Credit the welcome bonus exactly once.
        bonus_cursor = conn.execute(
            """INSERT INTO transactions
               (user_id, kind, amount, details, status)
               VALUES (?, 'DEPOSIT', 500, ?, 'APPROVED')""",
            (
                new_user_id,
                "₦500 new-user welcome bonus",
            )
        )

        conn.execute(
            """INSERT INTO welcome_bonuses (user_id, transaction_id)
               VALUES (?, ?)""",
            (new_user_id, bonus_cursor.lastrowid)
        )

        conn.commit()
        session.pop("pending_referral_code", None)

        flash(
            "Account created successfully! Your ₦500 welcome bonus "
            "has been credited. You can now log in.",
            "success"
        )
        return redirect(url_for("login"))

    except sqlite3.IntegrityError:
        conn.rollback()
        flash(
            "That username or phone number is already registered.",
            "error"
        )
        return redirect(url_for("register"))

    except Exception:
        conn.rollback()
        app.logger.exception("Registration failed")
        flash("Registration failed. Please try again.", "error")
        return redirect(url_for("register"))

    finally:
        conn.close()


''',
    "registration route",
)


# 3. Credit the inviter 7% when a plan purchase succeeds.
replace_section(
    r'@app\.route\("/select-plan", methods=\["POST"\]\)\s*'
    r'def select_plan\(\):.*?(?=\ninit_transaction_db\(\))',
    '''@app.route("/select-plan", methods=["POST"])
def select_plan():
    if "user_id" not in session:
        return redirect(url_for("login"))

    try:
        amount = int(request.form.get("amount", "0"))
        if amount not in [plan["amount"] for plan in PLANS]:
            raise ValueError
    except (ValueError, TypeError):
        flash("Please select a valid plan.", "error")
        return redirect(url_for("dashboard"))

    user_id = session["user_id"]
    conn = db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        approved = conn.execute(
            """SELECT kind, amount FROM transactions
               WHERE user_id = ? AND lower(status) = 'approved'""",
            (user_id,)
        ).fetchall()

        balance = 0.0
        for tx in approved:
            kind = (tx["kind"] or "").lower()
            value = float(tx["amount"] or 0)

            if "deposit" in kind:
                balance += value
            elif "withdraw" in kind or "plan_purchase" in kind:
                balance -= value

        pending_withdrawals = conn.execute(
            """SELECT COALESCE(SUM(amount), 0)
               FROM transactions
               WHERE user_id = ?
                 AND lower(status) = 'pending'
                 AND lower(kind) LIKE '%withdraw%'""",
            (user_id,)
        ).fetchone()[0]

        balance -= float(pending_withdrawals or 0)

        if balance < amount:
            conn.rollback()
            flash(
                f"Insufficient available balance: "
                f"₦{max(0, balance):,.2f}.",
                "error"
            )
            return redirect(url_for("dashboard"))

        # Record the approved plan purchase.
        purchase = conn.execute(
            """INSERT INTO transactions
               (user_id, kind, amount, details, status)
               VALUES (?, ?, ?, ?, 'APPROVED')""",
            (
                user_id,
                "PLAN_PURCHASE",
                amount,
                "Plan purchase; daily rewards are simulated, not guaranteed",
            )
        )
        plan_transaction_id = purchase.lastrowid

        # Find the person who invited this user.
        user = conn.execute(
            "SELECT referred_by FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        referral_message = ""

        if user and user["referred_by"]:
            inviter_id = user["referred_by"]

            # A plan transaction may generate only one referral reward.
            existing = conn.execute(
                """SELECT 1 FROM referral_rewards
                   WHERE plan_transaction_id = ?""",
                (plan_transaction_id,)
            ).fetchone()

            if not existing:
                reward = round(amount * 0.07, 2)

                # Use DEPOSIT so the existing balance calculations
                # include the referral reward.
                reward_tx = conn.execute(
                    """INSERT INTO transactions
                       (user_id, kind, amount, details, status)
                       VALUES (?, 'DEPOSIT', ?, ?, 'APPROVED')""",
                    (
                        inviter_id,
                        reward,
                        f"7% referral reward for plan purchase "
                        f"#{plan_transaction_id} by user #{user_id}",
                    )
                )

                conn.execute(
                    """INSERT INTO referral_rewards
                       (plan_transaction_id, inviter_user_id,
                        referred_user_id, reward_amount, transaction_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        plan_transaction_id,
                        inviter_id,
                        user_id,
                        reward,
                        reward_tx.lastrowid,
                    )
                )

                referral_message = (
                    f" Your inviter received a referral reward "
                    f"of ₦{reward:,.2f}."
                )

        conn.commit()

        flash(
            f"Plan purchase recorded for ₦{amount:,.0f}."
            " Daily rewards are simulated, not guaranteed."
            + referral_message,
            "success"
        )
        return redirect(url_for("dashboard"))

    except Exception:
        conn.rollback()
        app.logger.exception("Plan purchase failed")
        flash("Could not process the plan purchase. Please try again.", "error")
        return redirect(url_for("dashboard"))

    finally:
        conn.close()


''',
    "plan purchase route",
)


# Save changes only if all expected sections were found.
try:
    path.write_text(source)
    py_compile.compile(str(path), doraise=True)
except Exception:
    path.write_text(original)
    raise

print("SUCCESS: Referral and welcome bonus code installed.")
print("SUCCESS: app.py compiled without syntax errors.")
print("Backup saved as:", backup)
print("Next: restart the website and test with a new test account.")

