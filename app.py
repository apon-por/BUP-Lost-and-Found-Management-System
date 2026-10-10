import os
import json
from datetime import datetime
from xml.sax.saxutils import escape

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from database import one, all_, run
from auth import token, login_required, admin_required
from config import UPLOAD_FOLDER


# =========================================================
# CONFIGURATION
# =========================================================

app = Flask(__name__)
CORS(app)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

REPORT_FOLDER = os.path.join(UPLOAD_FOLDER, "reports")
os.makedirs(REPORT_FOLDER, exist_ok=True)

MAX_IMAGE_SIZE = 5 * 1024 * 1024

app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_SIZE

ROLES = ["student", "faculty", "officer", "staff", "admin"]

STATUSES = [
    "Lost",
    "Found",
    "Claimed",
    "Returned",
    "Closed",
]

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}


# =========================================================
# COMMON ITEM QUERY
# =========================================================

ITEM_SELECT = """
    SELECT
        i.item_id,
        i.item_type,
        i.user_id,
        u.name AS reporter_name,
        u.email AS reporter_email,
        i.category_id,
        c.category_name,
        i.item_name,
        i.description,
        i.item_date,
        i.location,
        i.image_path,
        i.status,
        i.verification_status,
        i.created_at
    FROM items i
    JOIN users u ON u.user_id = i.user_id
    LEFT JOIN categories c ON c.category_id = i.category_id
"""


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def api_error(message, status=400):
    return jsonify(error=message), status


def notify(user_id, message):
    run(
        """
        INSERT INTO notifications (user_id, message)
        VALUES (%s, %s)
        """,
        (user_id, message),
    )


def log_activity(user_id, action, module, description, reference_id=None):
    """Save an activity if the activity_history table exists."""
    try:
        run(
            """
            INSERT INTO activity_history
                (user_id, action, module, description, reference_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user_id, action, module, description, reference_id),
        )
    except Exception:
        # Activity logging must not break an otherwise successful operation.
        app.logger.exception("Activity history could not be saved")


def public_user(user):
    return {
        key: user[key]
        for key in [
            "user_id",
            "name",
            "email",
            "role",
            "phone",
            "department",
            "created_at",
        ]
    }


def save_image(file):
    if not file or not file.filename:
        return None

    if "." not in file.filename:
        raise ValueError("The image file must have an extension.")

    extension = file.filename.rsplit(".", 1)[1].lower()

    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Use PNG, JPG, JPEG or WEBP images only.")

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)

    if size > MAX_IMAGE_SIZE:
        raise ValueError("The image must not exceed 5 MB.")

    filename = (
        datetime.now().strftime("%Y%m%d%H%M%S%f")
        + "_"
        + secure_filename(file.filename)
    )

    file.save(os.path.join(UPLOAD_FOLDER, filename))
    return filename


def get_summary():
    return one(
        """
        SELECT
            (SELECT COUNT(*) FROM users) AS total_users,
            (SELECT COUNT(*) FROM items
                WHERE item_type = 'lost') AS total_lost,
            (SELECT COUNT(*) FROM items
                WHERE item_type = 'found') AS total_found,
            (SELECT COUNT(*) FROM claims) AS total_claims,
            (SELECT COUNT(*) FROM claims
                WHERE status = 'Pending') AS pending_claims,
            (SELECT COUNT(*) FROM items
                WHERE status = 'Returned') AS returned_items,
            (SELECT COUNT(*) FROM items
                WHERE verification_status = 'Pending') AS pending_reports
        """
    )


def build_item_query(filters):
    sql = """
        SELECT
            i.item_id,
            i.item_name,
            i.item_type,
            i.category_id,
            c.category_name,
            i.location,
            i.item_date,
            i.status,
            i.verification_status,
            u.name AS reporter_name,
            i.created_at
        FROM items i
        JOIN users u ON u.user_id = i.user_id
        LEFT JOIN categories c ON c.category_id = i.category_id
        WHERE 1=1
    """

    params = []

    if filters.get("type"):
        sql += " AND i.item_type = %s"
        params.append(filters["type"])

    if filters.get("category"):
        sql += " AND i.category_id = %s"
        params.append(filters["category"])

    if filters.get("location"):
        sql += " AND i.location LIKE %s"
        params.append("%" + filters["location"] + "%")

    if filters.get("status"):
        sql += " AND i.status = %s"
        params.append(filters["status"])

    if filters.get("verification_status"):
        sql += " AND i.verification_status = %s"
        params.append(filters["verification_status"])

    if filters.get("start_date"):
        sql += " AND i.item_date >= %s"
        params.append(filters["start_date"])

    if filters.get("end_date"):
        sql += " AND i.item_date <= %s"
        params.append(filters["end_date"])

    sql += " ORDER BY i.created_at DESC"

    return sql, params


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/api/health")
def health():
    return jsonify(
        status="ok",
        service="BUP Lost & Found API",
    )


# =========================================================
# AUTHENTICATION
# =========================================================

@app.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role = (data.get("role") or "student").lower()

    if not name or not email or not password:
        return api_error("Name, email and password are required.")

    if len(password) < 8:
        return api_error("Password must contain at least 8 characters.")

    # Public registration cannot create admin accounts.
    if role not in ROLES[:-1]:
        return api_error("Invalid user role.")

    if one("SELECT user_id FROM users WHERE email=%s", (email,)):
        return api_error("Email already registered.", 409)

    user_id = run(
        """
        INSERT INTO users
            (name, email, password, role, phone, department)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            name,
            email,
            generate_password_hash(password),
            role,
            data.get("phone"),
            data.get("department"),
        ),
    )

    user = one(
        """
        SELECT user_id, name, email, role, phone, department, created_at
        FROM users
        WHERE user_id=%s
        """,
        (user_id,),
    )

    log_activity(
        user_id, "Register", "Authentication", "Account created"
    )

    return jsonify(
        user=public_user(user),
        token=token(user),
    ), 201


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = one(
        "SELECT * FROM users WHERE email=%s",
        (email,),
    )

    if not user or not check_password_hash(user["password"], password):
        return api_error("Invalid email or password.", 401)

    log_activity(
        user["user_id"], "Login", "Authentication", "User logged in"
    )

    return jsonify(
        user=public_user(user),
        token=token(user),
    )


# =========================================================
# USER PROFILE
# =========================================================

@app.get("/api/users/me")
@login_required
def me(user):
    return jsonify(public_user(user))


@app.put("/api/users/me")
@login_required
def edit_me(user):
    data = request.get_json(silent=True) or {}

    name = (data.get("name") or user["name"]).strip()

    if not name:
        return api_error("Name cannot be empty.")

    run(
        """
        UPDATE users
        SET name=%s, phone=%s, department=%s
        WHERE user_id=%s
        """,
        (
            name,
            data.get("phone", user.get("phone")),
            data.get("department", user.get("department")),
            user["user_id"],
        ),
    )

    log_activity(
        user["user_id"], "Update", "Profile", "Profile updated"
    )

    return jsonify(message="Profile updated successfully.")


# =========================================================
# CATEGORIES
# =========================================================

@app.get("/api/categories")
def get_categories():
    return jsonify(
        all_("SELECT * FROM categories ORDER BY category_name")
    )


@app.post("/api/categories")
@login_required
@admin_required
def add_category(user):
    data = request.get_json(silent=True) or {}
    name = (data.get("category_name") or "").strip()

    if not name:
        return api_error("Category name is required.")

    category_id = run(
        "INSERT INTO categories (category_name) VALUES (%s)",
        (name,),
    )

    return jsonify(
        category_id=category_id,
        category_name=name,
    ), 201


@app.put("/api/categories/<int:category_id>")
@login_required
@admin_required
def edit_category(user, category_id):
    data = request.get_json(silent=True) or {}
    name = (data.get("category_name") or "").strip()

    if not name:
        return api_error("Category name is required.")

    run(
        """
        UPDATE categories
        SET category_name=%s
        WHERE category_id=%s
        """,
        (name, category_id),
    )

    return jsonify(message="Category updated.")


@app.delete("/api/categories/<int:category_id>")
@login_required
@admin_required
def delete_category(user, category_id):
    run(
        "DELETE FROM categories WHERE category_id=%s",
        (category_id,),
    )
    return jsonify(message="Category deleted.")


# =========================================================
# LOST / FOUND REPORT CREATION
# =========================================================

def create_report(user, item_type):
    data = request.form

    required = [
        "item_name",
        "category_id",
        "description",
        "item_date",
        "location",
    ]

    if any(not (data.get(key) or "").strip() for key in required):
        return api_error(
            "Item name, category, description, date and location are required."
        )

    category = one(
        "SELECT category_id FROM categories WHERE category_id=%s",
        (data["category_id"],),
    )

    if not category:
        return api_error("Please select a valid category.")

    try:
        image_name = save_image(request.files.get("image"))
    except ValueError as exc:
        return api_error(str(exc))

    status = "Lost" if item_type == "lost" else "Found"

    item_id = run(
        """
        INSERT INTO items
            (
                item_type, user_id, category_id, item_name,
                description, item_date, location, image_path,
                status, verification_status
            )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            item_type,
            user["user_id"],
            data["category_id"],
            data["item_name"].strip(),
            data["description"].strip(),
            data["item_date"],
            data["location"].strip(),
            image_name,
            status,
            "Pending",
        ),
    )

    log_activity(
        user["user_id"],
        "Create",
        "Items",
        f"{item_type.title()} report submitted",
        item_id,
    )

    return jsonify(
        message="Report submitted for admin verification.",
        item_id=item_id,
    ), 201


@app.post("/api/lost")
@login_required
def lost(user):
    return create_report(user, "lost")


@app.post("/api/found")
@login_required
def found(user):
    return create_report(user, "found")


# =========================================================
# PUBLIC ITEM SEARCH AND DETAILS
# =========================================================

@app.get("/api/items")
def items():
    args = request.args
    sql = ITEM_SELECT + " WHERE i.verification_status='Approved'"
    params = []

    if args.get("q"):
        sql += " AND (i.item_name LIKE %s OR i.description LIKE %s)"
        keyword = "%" + args["q"] + "%"
        params.extend([keyword, keyword])

    if args.get("category"):
        sql += " AND i.category_id=%s"
        params.append(args["category"])

    if args.get("location"):
        sql += " AND i.location LIKE %s"
        params.append("%" + args["location"] + "%")

    if args.get("date"):
        sql += " AND i.item_date=%s"
        params.append(args["date"])

    if args.get("type"):
        sql += " AND i.item_type=%s"
        params.append(args["type"])

    if args.get("status"):
        sql += " AND i.status=%s"
        params.append(args["status"])

    sql += " ORDER BY i.created_at DESC"

    return jsonify(all_(sql, params))


@app.get("/api/items/<int:item_id>")
def get_item(item_id):
    item = one(
        ITEM_SELECT + " WHERE i.item_id=%s",
        (item_id,),
    )

    if not item:
        return api_error("Item not found.", 404)

    return jsonify(item)


# =========================================================
# USER'S OWN ITEMS
# =========================================================

@app.get("/api/my-items")
@login_required
def my_items(user):
    return jsonify(
        all_(
            ITEM_SELECT
            + """
            WHERE i.user_id=%s
            ORDER BY i.created_at DESC
            """,
            (user["user_id"],),
        )
    )


@app.put("/api/items/<int:item_id>")
@login_required
def edit_item(user, item_id):
    item = one(
        "SELECT * FROM items WHERE item_id=%s",
        (item_id,),
    )

    if not item:
        return api_error("Item not found.", 404)

    if (
        item["user_id"] != user["user_id"]
        and user["role"] != "admin"
    ):
        return api_error("Not authorized.", 403)

    data = request.get_json(silent=True) or {}

    run(
        """
        UPDATE items
        SET item_name=%s, category_id=%s, description=%s,
            item_date=%s, location=%s
        WHERE item_id=%s
        """,
        (
            data.get("item_name", item["item_name"]),
            data.get("category_id", item["category_id"]),
            data.get("description", item["description"]),
            data.get("item_date", item["item_date"]),
            data.get("location", item["location"]),
            item_id,
        ),
    )

    log_activity(
        user["user_id"], "Update", "Items", "Item report updated", item_id
    )

    return jsonify(message="Item updated.")


# =========================================================
# CLAIMS
# =========================================================

@app.post("/api/claims")
@login_required
def claim(user):
    data = request.get_json(silent=True) or {}

    item_id = data.get("item_id")
    verification_info = (data.get("verification_info") or "").strip()

    if not item_id or not verification_info:
        return api_error(
            "Item ID and ownership verification information are required."
        )

    item = one(
        """
        SELECT *
        FROM items
        WHERE item_id=%s AND item_type='found'
        """,
        (item_id,),
    )

    if not item:
        return api_error("Found item not found.", 404)

    if item["user_id"] == user["user_id"]:
        return api_error("You cannot claim an item you reported.")

    claim_id = run(
        """
        INSERT INTO claims (item_id, claimant_id, verification_info)
        VALUES (%s, %s, %s)
        """,
        (item["item_id"], user["user_id"], verification_info),
    )

    log_activity(
        user["user_id"], "Create", "Claims", "Claim submitted", claim_id
    )

    return jsonify(
        message="Claim submitted.",
        claim_id=claim_id,
    ), 201


@app.get("/api/claims/my")
@login_required
def my_claims(user):
    return jsonify(
        all_(
            """
            SELECT cl.*, i.item_name, i.location
            FROM claims cl
            JOIN items i ON i.item_id=cl.item_id
            WHERE cl.claimant_id=%s
            ORDER BY cl.created_at DESC
            """,
            (user["user_id"],),
        )
    )


@app.get("/api/claims/<int:claim_id>")
@login_required
def get_claim(user, claim_id):
    claim_record = one(
        """
        SELECT cl.*, i.item_name, i.description, i.location
        FROM claims cl
        JOIN items i ON i.item_id=cl.item_id
        WHERE cl.claim_id=%s
        """,
        (claim_id,),
    )

    if not claim_record:
        return api_error("Claim not found.", 404)

    if (
        claim_record["claimant_id"] != user["user_id"]
        and user["role"] != "admin"
    ):
        return api_error("Not authorized.", 403)

    return jsonify(claim_record)


# =========================================================
# NOTIFICATIONS
# =========================================================

@app.get("/api/notifications")
@login_required
def notifications(user):
    return jsonify(
        all_(
            """
            SELECT *
            FROM notifications
            WHERE user_id=%s
            ORDER BY created_at DESC
            """,
            (user["user_id"],),
        )
    )


@app.put("/api/notifications/<int:notification_id>/read")
@login_required
def mark_notification_read(user, notification_id):
    run(
        """
        UPDATE notifications
        SET is_read=1
        WHERE notification_id=%s AND user_id=%s
        """,
        (notification_id, user["user_id"]),
    )

    return jsonify(message="Notification marked as read.")


# =========================================================
# ACTIVITY HISTORY
# =========================================================

@app.get("/api/history")
@login_required
def user_history(user):
    activities = all_(
        """
        SELECT
            activity_id, action, module, description,
            reference_id, created_at
        FROM activity_history
        WHERE user_id=%s
        ORDER BY created_at DESC
        LIMIT 200
        """,
        (user["user_id"],),
    )

    return jsonify(activities)


@app.get("/api/admin/history")
@login_required
@admin_required
def admin_history(user):
    activities = all_(
        """
        SELECT
            ah.activity_id,
            ah.user_id,
            COALESCE(u.name, 'Deleted User') AS user_name,
            ah.action,
            ah.module,
            ah.description,
            ah.reference_id,
            ah.created_at
        FROM activity_history ah
        LEFT JOIN users u ON u.user_id=ah.user_id
        ORDER BY ah.created_at DESC
        LIMIT 500
        """
    )

    return jsonify(activities)


# =========================================================
# ADMIN: USERS
# =========================================================

@app.get("/api/admin/users")
@login_required
@admin_required
def admin_users(user):
    return jsonify(
        all_(
            """
            SELECT user_id, name, email, role, phone, department, created_at
            FROM users
            ORDER BY created_at DESC
            """
        )
    )


@app.put("/api/admin/users/<int:user_id>")
@login_required
@admin_required
def update_admin_user(user, user_id):
    data = request.get_json(silent=True) or {}

    target = one(
        "SELECT * FROM users WHERE user_id=%s",
        (user_id,),
    )

    if not target:
        return api_error("User not found.", 404)

    role = data.get("role", target["role"])

    if role not in ROLES:
        return api_error("Invalid role.")

    if user_id == user["user_id"] and role != "admin":
        return api_error("You cannot remove your own admin role.")

    run(
        """
        UPDATE users
        SET name=%s, role=%s, phone=%s, department=%s
        WHERE user_id=%s
        """,
        (
            data.get("name", target["name"]),
            role,
            data.get("phone", target.get("phone")),
            data.get("department", target.get("department")),
            user_id,
        ),
    )

    return jsonify(message="User updated.")


@app.delete("/api/admin/users/<int:user_id>")
@login_required
@admin_required
def delete_admin_user(user, user_id):
    if user_id == user["user_id"]:
        return api_error("You cannot delete your own admin account.")

    target = one(
        "SELECT user_id FROM users WHERE user_id=%s",
        (user_id,),
    )

    if not target:
        return api_error("User not found.", 404)

    try:
        run("DELETE FROM users WHERE user_id=%s", (user_id,))
    except Exception:
        app.logger.exception("User deletion failed")
        return api_error("User has related records and cannot be deleted.", 409)

    return jsonify(message="User deleted.")


# =========================================================
# ADMIN: ITEMS
# =========================================================

@app.get("/api/admin/items")
@login_required
@admin_required
def admin_items(user):
    return jsonify(
        all_(ITEM_SELECT + " ORDER BY i.created_at DESC")
    )


@app.put("/api/admin/items/<int:item_id>/verify")
@login_required
@admin_required
def verify_item(user, item_id):
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")

    if decision not in ("Approved", "Rejected"):
        return api_error("Decision must be Approved or Rejected.")

    item = one(
        "SELECT user_id, item_name FROM items WHERE item_id=%s",
        (item_id,),
    )

    if not item:
        return api_error("Item not found.", 404)

    run(
        """
        UPDATE items
        SET verification_status=%s
        WHERE item_id=%s
        """,
        (decision, item_id),
    )

    notify(
        item["user_id"],
        f"Your report '{item['item_name']}' was {decision.lower()}.",
    )

    log_activity(
        user["user_id"],
        decision,
        "Items",
        f"Report {item_id} {decision.lower()}",
        item_id,
    )

    return jsonify(message=f"Report {decision.lower()}.")


# =========================================================
# ADMIN: CLAIMS
# =========================================================

@app.get("/api/admin/claims")
@login_required
@admin_required
def admin_claims(user):
    return jsonify(
        all_(
            """
            SELECT
                cl.*, i.item_name, i.location,
                u.name AS claimant_name,
                u.email AS claimant_email
            FROM claims cl
            JOIN items i ON i.item_id=cl.item_id
            JOIN users u ON u.user_id=cl.claimant_id
            ORDER BY cl.created_at DESC
            """
        )
    )


@app.put("/api/admin/claims/<int:claim_id>")
@login_required
@admin_required
def review_claim(user, claim_id):
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")

    if decision not in ("Approved", "Rejected"):
        return api_error("Decision must be Approved or Rejected.")

    claim_record = one(
        "SELECT * FROM claims WHERE claim_id=%s",
        (claim_id,),
    )

    if not claim_record:
        return api_error("Claim not found.", 404)

    if claim_record["status"] != "Pending":
        return api_error("This claim has already been reviewed.", 409)

    run(
        """
        UPDATE claims
        SET status=%s, reviewed_by=%s, reviewed_at=NOW()
        WHERE claim_id=%s
        """,
        (decision, user["user_id"], claim_id),
    )

    if decision == "Approved":
        run(
            "UPDATE items SET status='Claimed' WHERE item_id=%s",
            (claim_record["item_id"],),
        )

    notify(
        claim_record["claimant_id"],
        f"Your claim #{claim_id} was {decision.lower()}.",
    )

    log_activity(
        user["user_id"],
        decision,
        "Claims",
        f"Claim {claim_id} {decision.lower()}",
        claim_id,
    )

    return jsonify(message=f"Claim {decision.lower()}.")


# =========================================================
# ADMIN: ITEM STATUS
# =========================================================

@app.put("/api/admin/items/<int:item_id>/status")
@login_required
@admin_required
def set_item_status(user, item_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status")

    if status not in STATUSES:
        return api_error("Invalid status.")

    item = one(
        "SELECT user_id, item_name FROM items WHERE item_id=%s",
        (item_id,),
    )

    if not item:
        return api_error("Item not found.", 404)

    run(
        "UPDATE items SET status=%s WHERE item_id=%s",
        (status, item_id),
    )

    notify(
        item["user_id"],
        f"Item '{item['item_name']}' status changed to {status}.",
    )

    log_activity(
        user["user_id"],
        "Update",
        "Items",
        f"Item {item_id} status changed to {status}",
        item_id,
    )

    return jsonify(message="Item status updated.")


# =========================================================
# REPORT CENTER
# =========================================================

@app.get("/api/reports/summary")
@login_required
@admin_required
def report_summary(user):
    return jsonify(get_summary())


@app.get("/api/reports/items")
@login_required
@admin_required
def item_report(user):
    return jsonify(
        all_(
            """
            SELECT status, item_type, COUNT(*) AS count
            FROM items
            GROUP BY status, item_type
            ORDER BY item_type, status
            """
        )
    )


@app.get("/api/reports/claims")
@login_required
@admin_required
def claims_report(user):
    return jsonify(
        all_(
            """
            SELECT status, COUNT(*) AS count
            FROM claims
            GROUP BY status
            ORDER BY status
            """
        )
    )


@app.get("/api/reports/data")
@login_required
@admin_required
def report_data(user):
    sql, params = build_item_query(request.args)
    return jsonify(all_(sql, params))


@app.get("/api/reports/monthly")
@login_required
@admin_required
def monthly_report(user):
    return jsonify(
        all_(
            """
            SELECT
                DATE_FORMAT(item_date, '%Y-%m') AS month,
                SUM(CASE WHEN item_type='lost' THEN 1 ELSE 0 END) AS lost,
                SUM(CASE WHEN item_type='found' THEN 1 ELSE 0 END) AS found,
                COUNT(*) AS total
            FROM items
            GROUP BY DATE_FORMAT(item_date, '%Y-%m')
            ORDER BY month
            """
        )
    )


@app.get("/api/reports/categories")
@login_required
@admin_required
def category_report(user):
    return jsonify(
        all_(
            """
            SELECT
                COALESCE(c.category_name, 'Uncategorized') AS category,
                COUNT(i.item_id) AS total
            FROM items i
            LEFT JOIN categories c ON c.category_id=i.category_id
            GROUP BY c.category_id, c.category_name
            ORDER BY total DESC
            """
        )
    )


@app.get("/api/reports/locations")
@login_required
@admin_required
def location_report(user):
    return jsonify(
        all_(
            """
            SELECT location, COUNT(*) AS total
            FROM items
            WHERE location IS NOT NULL AND location <> ''
            GROUP BY location
            ORDER BY total DESC
            """
        )
    )


# =========================================================
# GENERATED REPORT HISTORY
# =========================================================

@app.post("/api/reports/history")
@login_required
@admin_required
def save_report_history(user):
    data = request.get_json(silent=True) or {}

    report_name = data.get("report_name") or "BUP Lost & Found Report"
    report_type = data.get("report_type") or "Summary"
    file_format = (data.get("file_format") or "PDF").upper()
    filters = data.get("filters") or {}

    report_id = run(
        """
        INSERT INTO generated_reports
            (
                report_name, report_type, generated_by,
                start_date, end_date, filters, file_format, file_path
            )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            report_name,
            report_type,
            user["user_id"],
            data.get("start_date") or None,
            data.get("end_date") or None,
            json.dumps(filters),
            file_format,
            data.get("file_path") or None,
        ),
    )

    return jsonify(
        message="Report history saved.",
        report_id=report_id,
    ), 201


@app.get("/api/reports/history")
@login_required
@admin_required
def report_history(user):
    return jsonify(
        all_(
            """
            SELECT
                gr.report_id,
                gr.report_name,
                gr.report_type,
                gr.start_date,
                gr.end_date,
                gr.filters,
                gr.file_format,
                gr.file_path,
                gr.generated_at,
                u.name AS generated_by_name,
                u.email AS generated_by_email
            FROM generated_reports gr
            JOIN users u ON u.user_id=gr.generated_by
            ORDER BY gr.generated_at DESC
            """
        )
    )


@app.get("/api/reports/history/<int:report_id>")
@login_required
@admin_required
def report_history_details(user, report_id):
    report = one(
        """
        SELECT
            gr.report_id,
            gr.report_name,
            gr.report_type,
            gr.start_date,
            gr.end_date,
            gr.filters,
            gr.file_format,
            gr.file_path,
            gr.generated_at,
            u.name AS generated_by_name,
            u.email AS generated_by_email
        FROM generated_reports gr
        JOIN users u ON u.user_id=gr.generated_by
        WHERE gr.report_id=%s
        """,
        (report_id,),
    )

    if not report:
        return api_error("Report not found.", 404)

    return jsonify(report)


# =========================================================
# PDF REPORT GENERATION
# Exactly ONE route/function definition
# =========================================================

@app.post("/api/reports/generate-pdf")
@login_required
@admin_required
def generate_pdf_report(user):
    data = request.get_json(silent=True) or {}
    filters = data.get("filters") or {}

    try:
        sql, params = build_item_query(filters)
        records = all_(sql, params)
        summary = get_summary()

        filename = (
            "BUP_Report_"
            + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            + ".pdf"
        )

        filepath = os.path.join(REPORT_FOLDER, filename)

        doc = SimpleDocTemplate(
            filepath,
            pagesize=landscape(A4),
            rightMargin=12 * mm,
            leftMargin=12 * mm,
            topMargin=15 * mm,
            bottomMargin=18 * mm,
            title="BUP Lost & Found Report",
            author=str(user["name"]),
        )

        styles = getSampleStyleSheet()
        story = []

        story.append(
            Paragraph(
                "BANGLADESH UNIVERSITY OF PROFESSIONALS",
                styles["Title"],
            )
        )

        story.append(
            Paragraph(
                "BUP LOST &amp; FOUND MANAGEMENT SYSTEM",
                styles["Heading2"],
            )
        )

        story.append(
            Paragraph(
                escape(
                    data.get("report_name")
                    or "Comprehensive Lost & Found Report"
                ),
                styles["Heading1"],
            )
        )

        story.append(
            Paragraph(
                "Generated by: " + escape(str(user["name"])),
                styles["Normal"],
            )
        )

        story.append(
            Paragraph(
                "Generated on: "
                + datetime.now().strftime("%d %B %Y, %I:%M %p"),
                styles["Normal"],
            )
        )

        story.append(Spacer(1, 10))

        summary_rows = [
            ["Metric", "Count"],
            ["Total Users", str(summary["total_users"])],
            ["Total Lost", str(summary["total_lost"])],
            ["Total Found", str(summary["total_found"])],
            ["Total Claims", str(summary["total_claims"])],
            ["Pending Claims", str(summary["pending_claims"])],
            ["Returned Items", str(summary["returned_items"])],
            ["Pending Reports", str(summary["pending_reports"])],
        ]

        summary_table = Table(
            summary_rows,
            colWidths=[65 * mm, 35 * mm],
        )

        summary_table.setStyle(
            TableStyle(
                [
                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, 0),
                        colors.HexColor("#17365D"),
                    ),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ("PADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )

        story.append(
            Paragraph("Summary Statistics", styles["Heading2"])
        )
        story.append(summary_table)
        story.append(Spacer(1, 14))

        story.append(
            Paragraph("Detailed Item Data", styles["Heading2"])
        )

        table_rows = [[
            "ID",
            "Item",
            "Type",
            "Category",
            "Location",
            "Date",
            "Status",
            "Verification",
            "Reporter",
        ]]

        for item in records:
            table_rows.append([
                str(item.get("item_id", "")),
                Paragraph(
                    escape(str(item.get("item_name") or "")),
                    styles["Normal"],
                ),
                str(item.get("item_type") or ""),
                Paragraph(
                    escape(str(item.get("category_name") or "N/A")),
                    styles["Normal"],
                ),
                Paragraph(
                    escape(str(item.get("location") or "")),
                    styles["Normal"],
                ),
                str(item.get("item_date") or ""),
                str(item.get("status") or ""),
                str(item.get("verification_status") or ""),
                Paragraph(
                    escape(str(item.get("reporter_name") or "")),
                    styles["Normal"],
                ),
            ])

        if not records:
            table_rows.append([
                "-",
                "No records found",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
            ])

        item_table = Table(
            table_rows,
            repeatRows=1,
            colWidths=[
                12 * mm,
                32 * mm,
                18 * mm,
                25 * mm,
                34 * mm,
                23 * mm,
                22 * mm,
                28 * mm,
                28 * mm,
            ],
        )

        item_table.setStyle(
            TableStyle(
                [
                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, 0),
                        colors.HexColor("#17365D"),
                    ),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("PADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )

        story.append(item_table)

        def add_footer(canvas, document):
            canvas.saveState()
            canvas.setFont("Helvetica", 8)
            canvas.drawCentredString(
                landscape(A4)[0] / 2,
                8 * mm,
                "BUP Lost & Found Management System | Page "
                + str(document.page),
            )
            canvas.restoreState()

        doc.build(
            story,
            onFirstPage=add_footer,
            onLaterPages=add_footer,
        )

        report_id = run(
            """
            INSERT INTO generated_reports
                (
                    report_name, report_type, generated_by,
                    start_date, end_date, filters,
                    file_format, file_path
                )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                data.get("report_name")
                or "BUP Lost & Found Report",
                data.get("report_type") or "Summary",
                user["user_id"],
                filters.get("start_date") or None,
                filters.get("end_date") or None,
                json.dumps(filters),
                "PDF",
                "reports/" + filename,
            ),
        )

        log_activity(
            user["user_id"],
            "Generate",
            "Reports",
            "PDF report generated",
            report_id,
        )

        return jsonify(
            message="PDF generated successfully.",
            report_id=report_id,
            filename=filename,
            download_url="/uploads/reports/" + filename,
        ), 201

    except Exception:
        app.logger.exception("PDF generation failed")
        return api_error(
            "PDF generation failed. Check the backend terminal.",
            500,
        )


# =========================================================
# SERVE UPLOADED IMAGES AND PDF REPORTS
# =========================================================

@app.get("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# =========================================================
# ERROR HANDLER
# =========================================================

@app.errorhandler(413)
def file_too_large(_error):
    return jsonify(
        error="The uploaded file is too large. Maximum size is 5 MB."
    ), 413


# =========================================================
# RUN APPLICATION
# =========================================================

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
    )