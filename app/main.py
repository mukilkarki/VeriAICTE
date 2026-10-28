import csv
import io
import json
import mimetypes
import os
import re
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import firebase_admin
from dotenv import load_dotenv
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials, firestore
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from google.auth.exceptions import DefaultCredentialsError
from google.api_core.exceptions import AlreadyExists
from google.api_core.exceptions import GoogleAPIError
from werkzeug.utils import secure_filename

from .ai_review import AIReviewError, run_openrouter_review
from .extract import SUPPORTED_IMAGE_EXTENSIONS, extract_file
from .rules import PATHWAYS, evaluate

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
ALLOWED = {
    ".pdf", *SUPPORTED_IMAGE_EXTENSIONS, ".txt", ".md", ".csv", ".json", ".xml", ".html",
}
MAX_FILE = 15 * 1024 * 1024
MAX_FIRESTORE_TEXT_BYTES = 700 * 1024
FIRESTORE_TEXT_CHUNK_SIZE = 100_000
REQUIRED_INSTITUTION_PROFILE_FIELDS = {
    "institution_name": "Institution name",
    "institution_type": "Institution type",
    "address": "Address",
    "state": "State",
    "district": "District",
    "pin_code": "PIN code",
    "phone": "Phone",
    "institution_email": "Institution email",
    "sponsoring_body_name": "Trust / Society / Company name",
    "sponsoring_body_address": "Trust / Society / Company address",
    "head_name": "Principal / Director name",
    "head_designation": "Principal / Director designation",
    "head_phone": "Principal / Director phone",
    "head_email": "Principal / Director email",
    "affiliation": "Affiliating University / Board",
    "official_website": "Official website",
    "mandatory_disclosure": "Mandatory disclosure information",
}
OPTIONAL_INSTITUTION_PROFILE_FIELDS = {
    "programmes": "Programmes, courses and approved intake",
    "faculty": "Faculty names, designations and qualifications",
    "infrastructure": "Department-wise facilities, laboratories and equipment",
    "accreditation": "NAAC / NBA status",
    "governance": "Organizational chart and grievance redressal mechanisms",
    "approval_records": "Current approval status, academic year and approval letter",
}
STATE_UTS = (
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
    "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
)
NEW_INSTITUTION_FIELDS = (
    "institution_name", "address", "state", "district", "pin_code",
    "institution_type", "minority_status", "minority_type", "website_url",
    "official_email", "landline_phone", "promoting_body_name",
    "registration_number", "registration_date", "trust_pan",
    "signatory_name", "signatory_designation", "signatory_mobile",
    "signatory_email", "bank_name", "bank_account_number", "ifsc_code",
    "land_ownership_type", "land_area_acres", "built_up_area_sq_meters",
)
EXISTING_INSTITUTION_FIELDS = (
    "hoi_name", "appointment_type", "hoi_mobile", "hoi_email",
    "affiliating_university", "affiliation_validity_year",
    "existing_approved_intake", "applied_intake_status", "total_faculty",
    "current_student_strength", "mandatory_disclosure_url",
    "fire_safety_expiry_date", "structural_stability_expiry_date",
    "anti_ragging_status",
)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
    MAX_CONTENT_LENGTH=40 * 1024 * 1024,
    UPLOAD_STORAGE_DIR=os.environ.get(
        "UPLOAD_STORAGE_DIR", str(ROOT / "instance" / "uploads")
    ),
)

_firebase_lock = threading.Lock()
_firebase_app = None
_firebase_services = None


class FirebaseSetupError(RuntimeError):
    pass


class TurnstileSetupError(RuntimeError):
    pass


class TurnstileVerificationError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stored_document_path(app_id, document):
    storage_key = document.get("storage_key")
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]+", app_id)
        or not isinstance(storage_key, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+\.[a-z0-9]+", storage_key)
    ):
        return None
    storage_root = Path(app.config["UPLOAD_STORAGE_DIR"]).resolve()
    document_path = (storage_root / app_id / storage_key).resolve()
    if not document_path.is_relative_to(storage_root):
        return None
    return document_path


def application_form_fields(application):
    profile = application.get("institution_profile") or {}
    details = (
        application.get("new_institution_details")
        if application.get("pathway") == "new"
        else application.get("existing_institution_details")
    ) or {}
    profile_labels = {
        "institution_name": "Institution name",
        "institution_type": "Institution type",
        "address": "Address",
        "state": "State / UT",
        "district": "District",
        "pin_code": "PIN code",
        "phone": "Phone",
        "institution_email": "Institution email",
        "sponsoring_body_name": "Promoting body",
        "sponsoring_body_address": "Promoting body address",
        "head_name": "Principal / Director",
        "head_designation": "Designation",
        "head_phone": "Head phone",
        "head_email": "Head email",
        "affiliation": "Affiliating University / Board",
        "official_website": "Official website",
        "mandatory_disclosure": "Mandatory disclosure",
        "programmes": "Programmes",
        "faculty": "Faculty",
        "infrastructure": "Infrastructure",
        "accreditation": "Accreditation",
        "governance": "Governance",
        "approval_records": "Approval records",
    }
    new_labels = {
        "institution_name": "Name of the Institution",
        "address": "Address & Location",
        "state": "State / UT",
        "district": "District",
        "pin_code": "PIN Code",
        "institution_type": "Institution Type",
        "minority_status": "Minority Status",
        "minority_type": "Minority Type",
        "website_url": "Website URL",
        "official_email": "Official Email ID",
        "landline_phone": "Landline / Alternate Phone",
        "promoting_body_name": "Name of the Promoting Body",
        "registration_number": "Registration Number",
        "registration_date": "Registration Date",
        "trust_pan": "PAN of Trust / Society",
        "signatory_name": "Authorized Signatory Name",
        "signatory_designation": "Signatory Designation",
        "signatory_mobile": "Signatory Mobile Number",
        "signatory_email": "Signatory Email ID",
        "bank_name": "Bank Name",
        "bank_account_number": "Bank Account Number",
        "ifsc_code": "IFSC Code",
        "land_ownership_type": "Land Ownership Type",
        "land_area_acres": "Total Land Area (acres)",
        "built_up_area_sq_meters": "Total Built-up Area (sq. meters)",
    }
    existing_labels = {
        "academic_application_id": "Current Academic Application ID",
        "hoi_name": "Name of Principal / Director",
        "appointment_type": "Appointment Type",
        "hoi_mobile": "Mobile Number of HOI",
        "hoi_email": "Email ID of HOI",
        "affiliating_university": "Affiliating University / Board",
        "affiliation_validity_year": "Affiliation Validity Year",
        "existing_approved_intake": "Existing Approved Intake (Per Course)",
        "applied_intake_status": "Applied Intake Status",
        "total_faculty": "Total Count of Appointed Faculty",
        "current_student_strength": "Total Enrolled Students",
        "mandatory_disclosure_url": "Mandatory Disclosure Web Link",
        "fire_safety_expiry_date": "Fire Safety Certificate Expiry Date",
        "structural_stability_expiry_date": "Structural Stability Certificate Expiry Date",
        "anti_ragging_status": "Anti-Ragging Committee Setup Status",
    }
    details_labels = new_labels if application.get("pathway") == "new" else existing_labels
    profile_fields = [
        (profile_labels.get(key, key.replace("_", " ").title()), value)
        for key, value in profile.items()
        if value not in (None, "")
    ]
    form_fields = [
        (details_labels.get(key, key.replace("_", " ").title()), value)
        for key, value in details.items()
        if value not in (None, "")
    ]
    if application.get("institution_pid"):
        form_fields.insert(0, ("AICTE Permanent ID (PID)", application["institution_pid"]))
    return profile_fields, form_fields


def create_application_pdf(application):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
    from xml.sax.saxutils import escape

    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Application {application.get('id', '')} form details",
    )
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "FormValue",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        alignment=TA_LEFT,
        wordWrap="CJK",
    )
    story = [
        Paragraph("VeriAICTE — Application Form Details", styles["Title"]),
        Spacer(1, 5 * mm),
        Paragraph(f"Application: {escape(str(application.get('title', '')))}", styles["Heading2"]),
        Paragraph(f"Reference: {escape(str(application.get('id', '')))}", body_style),
        Paragraph(f"Pathway: {escape(PATHWAYS.get(application.get('pathway'), 'Unknown'))}", body_style),
        Paragraph(f"Institution: {escape(str(application.get('institution_name', '')))}", body_style),
        Paragraph(f"Status: {escape(str(application.get('status', '')))}", body_style),
        Spacer(1, 5 * mm),
    ]
    profile_fields, form_fields = application_form_fields(application)
    for heading, fields in (
        ("Saved Institution Profile", profile_fields),
        ("Submitted Application Form", form_fields),
    ):
        story.append(Paragraph(heading, styles["Heading2"]))
        if not fields:
            story.append(Paragraph("No details recorded.", body_style))
            continue
        rows = [
            [
                Paragraph(f"<b>{escape(str(label))}</b>", body_style),
                Paragraph(escape(str(value)).replace("\n", "<br/>"), body_style),
            ]
            for label, value in fields
        ]
        table = Table(rows, colWidths=(52 * mm, 118 * mm), repeatRows=0, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef5f1")),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dce7e2")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.extend((table, Spacer(1, 4 * mm)))
    document.build(story)
    output.seek(0)
    return output


def institution_profile_complete(profile):
    return bool(profile.get("pid")) and all(
        str(profile.get(field, "")).strip()
        for field in REQUIRED_INSTITUTION_PROFILE_FIELDS
    )


def assign_institution_pid(client, user_ref):
    year = datetime.now(timezone.utc).year
    counter_ref = client.collection("counters").document(f"institution_pid_{year}")
    transaction = client.transaction()

    @firestore.transactional
    def assign(transaction):
        user_snapshot = user_ref.get(transaction=transaction)
        user_profile = user_snapshot.to_dict() or {}
        if user_profile.get("pid"):
            return user_profile["pid"]

        counter_snapshot = counter_ref.get(transaction=transaction)
        sequence = (counter_snapshot.to_dict() or {}).get("count", 0) + 1
        pid = f"AICTE/ID/{year}/{sequence:03d}"
        transaction.set(counter_ref, {"count": sequence})
        transaction.update(user_ref, {"pid": pid})
        return pid

    return assign(transaction)


def firebase_config():
    return {
        "apiKey": os.environ.get("FIREBASE_API_KEY", ""),
        "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
        "projectId": os.environ.get("FIREBASE_PROJECT_ID", ""),
        "appId": os.environ.get("FIREBASE_APP_ID", ""),
    }


def verify_turnstile(token, action):
    secret_key = os.environ.get("CLOUDFLARE_TURNSTILE_SECRET_KEY", "").strip()
    if not secret_key:
        raise TurnstileSetupError(
            "Cloudflare Turnstile is not configured. Set CLOUDFLARE_TURNSTILE_SECRET_KEY."
        )
    if not isinstance(token, str) or not token.strip():
        return False

    form_data = {"secret": secret_key, "response": token.strip()}
    if request.remote_addr:
        form_data["remoteip"] = request.remote_addr
    verify_request = Request(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data=urlencode(form_data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(verify_request, timeout=5) as response:
            result = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        app.logger.exception("Cloudflare Turnstile verification request failed")
        raise TurnstileVerificationError(
            "Could not verify the security challenge. Please try again."
        ) from error
    return (
        isinstance(result, dict)
        and result.get("success") is True
        and result.get("action") == action
    )


def firebase_admin_app():
    global _firebase_app
    if _firebase_app:
        return _firebase_app
    with _firebase_lock:
        if _firebase_app:
            return _firebase_app

        project_id = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
        if not project_id:
            raise FirebaseSetupError(
                "Firebase is not configured. Set FIREBASE_PROJECT_ID and the Firebase web app settings in .env."
            )

        service_account_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
        if service_account_json:
            try:
                credential = credentials.Certificate(json.loads(service_account_json))
            except (json.JSONDecodeError, ValueError) as error:
                raise FirebaseSetupError(
                    "FIREBASE_SERVICE_ACCOUNT_JSON must contain valid Firebase service-account JSON."
                ) from error
        else:
            try:
                credential = credentials.ApplicationDefault()
            except DefaultCredentialsError as error:
                raise FirebaseSetupError(
                    "Firebase Admin credentials are missing. Set GOOGLE_APPLICATION_CREDENTIALS "
                    "or FIREBASE_SERVICE_ACCOUNT_JSON."
                ) from error

        try:
            _firebase_app = firebase_admin.get_app()
        except ValueError:
            _firebase_app = firebase_admin.initialize_app(credential, {"projectId": project_id})
        return _firebase_app


def firestore_db():
    global _firebase_services
    if _firebase_services:
        return _firebase_services[1]

    firebase_app = firebase_admin_app()
    with _firebase_lock:
        if _firebase_services:
            return _firebase_services[1]
        client = firestore.client(app=firebase_app)
        _bootstrap_admin(client)
        _firebase_services = (firebase_auth, client)
        return client


def _bootstrap_admin(client):
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    if not email:
        return

    try:
        admin_user = firebase_auth.get_user_by_email(email)
    except firebase_auth.UserNotFoundError:
        password = os.environ.get("ADMIN_PASSWORD", "")
        if not password:
            raise RuntimeError(
                "The configured ADMIN_EMAIL does not exist in Firebase Authentication. "
                "Create it in Firebase Console or set ADMIN_PASSWORD for first-run provisioning."
            )
        try:
            admin_user = firebase_auth.create_user(
                email=email,
                password=password,
                display_name="System Administrator",
            )
        except firebase_auth.EmailAlreadyExistsError:
            admin_user = firebase_auth.get_user_by_email(email)

    profile_ref = client.collection("users").document(admin_user.uid)
    profile = profile_ref.get()
    profile_data = profile.to_dict() or {}
    profile_data.update(
        {
            "name": profile_data.get("name") or admin_user.display_name or "System Administrator",
            "email": admin_user.email or email,
            "role": "admin",
            "institution_name": profile_data.get("institution_name", ""),
            "active": profile_data.get("active", True),
            "created_at": profile_data.get("created_at") or now(),
        }
    )
    profile_ref.set(profile_data)


def user():
    uid = session.get("uid")
    if not uid:
        return None
    if session.get("auth_provider") != "firebase" or not isinstance(uid, str):
        session.clear()
        return None
    profile = firestore_db().collection("users").document(uid).get()
    if not profile.exists:
        return None
    data = profile.to_dict()
    if not data.get("active", True):
        return None
    return {"id": profile.id, **data}


def collection_data(collection, order_by=None, descending=False, limit=None):
    query = firestore_db().collection(collection)
    if order_by:
        direction = firestore.Query.DESCENDING if descending else firestore.Query.ASCENDING
        query = query.order_by(order_by, direction=direction)
    if limit:
        query = query.limit(limit)
    return [{"id": doc.id, **(doc.to_dict() or {})} for doc in query.stream()]


def application_data(app_id):
    doc = firestore_db().collection("applications").document(app_id).get()
    if not doc.exists:
        abort(404)
    data = {"id": doc.id, **(doc.to_dict() or {})}
    submitter = firestore_db().collection("users").document(data["user_id"]).get()
    data["submitter_email"] = (submitter.to_dict() or {}).get("email", "") if submitter.exists else ""
    return data


def audit(action, target_type, target_id=None, details=""):
    actor = session.get("uid")
    email = session.get("email", "system")
    firestore_db().collection("audit_logs").add(
        {
            "actor_id": actor,
            "actor_email": email,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "details": details[:1500],
            "created_at": now(),
        }
    )


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        current_user = user()
        if not current_user:
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("login"))
        if (
            current_user.get("role") == "institution"
            and not institution_profile_complete(current_user)
            and request.endpoint != "profile"
        ):
            flash("Complete your institution profile before continuing.", "warning")
            return redirect(url_for("profile"))
        return fn(*args, **kwargs)

    return wrapped


def roles(*allowed):
    def decorate(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if user()["role"] not in allowed:
                abort(403)
            return fn(*args, **kwargs)

        return wrapped

    return decorate


def get_app(app_id):
    application = application_data(app_id)
    current_user = user()
    if (
        current_user["role"] not in ("admin", "reviewer")
        and application["user_id"] != current_user["id"]
    ):
        abort(403)
    return application


@app.context_processor
def globals():
    return {
        "current_user": user(),
        "pathways": PATHWAYS,
        "current_year": datetime.now().year,
        "firebase_config": firebase_config(),
        "turnstile_site_key": os.environ.get(
            "CLOUDFLARE_TURNSTILE_SITE_KEY", ""
        ).strip(),
        "ai_review_configured": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
        "ai_review_model": os.environ.get("OPENROUTER_MODEL", "").strip() or "openrouter/free",
        "image_extensions": SUPPORTED_IMAGE_EXTENSIONS,
        "upload_extensions": tuple(sorted(ALLOWED)),
    }


@app.route("/")
def index():
    current_user = user()
    if not current_user:
        return render_template("landing.html")
    endpoint = "admin_dashboard" if current_user["role"] in ("admin", "reviewer") else "institution_dashboard"
    return redirect(url_for(endpoint))


@app.route("/register")
def register():
    return render_template("auth.html", mode="register")


@app.route("/login")
def login():
    return render_template("auth.html", mode="login")


@app.route("/auth/turnstile", methods=["POST"])
def verify_auth_turnstile():
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify(error="Invalid security challenge request."), 400

    mode = data.get("mode")
    if mode not in ("login", "register"):
        return jsonify(error="Invalid security challenge request."), 400

    try:
        challenge_verified = verify_turnstile(
            data.get("turnstileToken"), mode.upper()
        )
    except TurnstileSetupError as error:
        return jsonify(error=str(error)), 503
    except TurnstileVerificationError as error:
        return jsonify(error=str(error)), 503
    if not challenge_verified:
        return jsonify(error="Complete the Cloudflare security challenge and try again."), 403

    session["turnstile_verified_at"] = time.time()
    session["turnstile_verified_action"] = mode.upper()
    return jsonify(verified=True)


@app.route("/auth/session", methods=["POST"])
def create_auth_session():
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify(error="Invalid sign-in request."), 400

    id_token = data.get("idToken")
    mode = data.get("mode")
    if not isinstance(id_token, str) or mode not in ("login", "register"):
        return jsonify(error="Invalid sign-in request."), 400

    challenge_verified_at = session.pop("turnstile_verified_at", None)
    challenge_action = session.pop("turnstile_verified_action", None)
    if (
        challenge_action != mode.upper()
        or not isinstance(challenge_verified_at, (int, float))
        or not 0 <= time.time() - challenge_verified_at <= 300
    ):
        return jsonify(error="Complete the Cloudflare security challenge and try again."), 403

    try:
        firebase_app = firebase_admin_app()
    except FirebaseSetupError as error:
        return jsonify(error=str(error)), 503
    except (firebase_admin.exceptions.FirebaseError, GoogleAPIError):
        app.logger.exception("Firebase Admin SDK initialization failed before token verification")
        return jsonify(
            error="Firebase Admin SDK could not initialize. Check the Firebase Admin credentials "
            "and FIREBASE_PROJECT_ID."
        ), 503

    try:
        claims = firebase_auth.verify_id_token(id_token, check_revoked=True, app=firebase_app)
    except (
        firebase_auth.InvalidIdTokenError,
        firebase_auth.ExpiredIdTokenError,
        firebase_auth.RevokedIdTokenError,
        firebase_auth.UserDisabledError,
        ValueError,
    ) as error:
        app.logger.warning(
            "Firebase ID token rejected (%s): %s",
            type(error).__name__,
            str(error),
        )
        return jsonify(error="Your sign-in could not be verified. Please try again."), 401
    except (DefaultCredentialsError, firebase_admin.exceptions.FirebaseError, GoogleAPIError):
        app.logger.exception("Firebase ID token verification failed")
        return jsonify(
            error="The server could not verify Firebase sign-in. Configure Firebase Admin credentials "
            "and check server access to Firebase."
        ), 503

    uid = claims.get("uid")
    email_claim = claims.get("email")
    email = email_claim.strip().lower() if isinstance(email_claim, str) else ""
    if not uid or not email:
        return jsonify(error="Your Firebase account must have an email address."), 400

    try:
        client = firestore_db()
    except FirebaseSetupError as error:
        return jsonify(error=str(error)), 503
    except (firebase_admin.exceptions.FirebaseError, GoogleAPIError) as error:
        app.logger.exception("Firebase initialization or account provisioning failed")
        return jsonify(
            error="Could not connect to Firebase. Check that the Firebase Admin service account "
            "has access to Firebase Authentication and Cloud Firestore."
        ), 503
    profile_ref = client.collection("users").document(uid)
    profile_doc = profile_ref.get()
    if mode == "register":
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            return jsonify(error="Enter your name."), 400
        name = name.strip()
        if not profile_doc.exists:
            try:
                profile_ref.create(
                    {
                        "name": name,
                        "email": email,
                        "role": "institution",
                        "institution_name": "",
                        "active": True,
                        "created_at": now(),
                    }
                )
            except AlreadyExists:
                pass
        profile_doc = profile_ref.get()
    elif not profile_doc.exists:
        profile_ref.set(
            {
                "name": claims.get("name") or email.split("@", 1)[0],
                "email": email,
                "role": "institution",
                "institution_name": "",
                "active": True,
                "created_at": now(),
            }
        )
        profile_doc = profile_ref.get()

    profile = profile_doc.to_dict() or {}
    if not profile.get("active", True):
        return jsonify(error="This account is disabled. Contact an administrator."), 403

    session.clear()
    session.update(uid=uid, email=email, auth_provider="firebase")
    audit("register" if mode == "register" else "login", "user", uid, "Account signed in")
    endpoint = "admin_dashboard" if profile.get("role") in ("admin", "reviewer") else "institution_dashboard"
    if profile.get("role") == "institution" and not institution_profile_complete(profile):
        endpoint = "profile"
    return jsonify(redirect=url_for(endpoint))


@app.route("/logout", methods=["POST"])
def logout():
    if session.get("uid"):
        audit("logout", "user", session["uid"], "Signed out")
    session.clear()
    return redirect(url_for("index"))


@app.route("/institution")
@roles("institution")
def institution_dashboard():
    current_user = user()
    applications = [
        item
        for item in collection_data("applications", "updated_at", descending=True)
        if item.get("user_id") == current_user["id"]
    ]
    counts = {}
    for application in applications:
        status = application["status"]
        counts[status] = counts.get(status, 0) + 1
    return render_template("institution.html", applications=applications, counts=counts, user=current_user)


@app.route("/institution/profile", methods=["GET", "POST"])
@roles("institution")
def profile():
    current_user = user()
    if request.method == "POST":
        values = {
            field: request.form.get(field, "").strip()
            for field in REQUIRED_INSTITUTION_PROFILE_FIELDS
        }
        values.update(
            {
                field: request.form.get(field, "").strip()
                for field in OPTIONAL_INSTITUTION_PROFILE_FIELDS
            }
        )
        values["institution_email"] = values["institution_email"].lower()
        values["head_email"] = values["head_email"].lower()
        missing_fields = [
            label
            for field, label in REQUIRED_INSTITUTION_PROFILE_FIELDS.items()
            if not values[field]
        ]
        if missing_fields:
            flash(f"Complete all required fields: {', '.join(missing_fields)}.", "danger")
            return render_template("profile.html", user={**current_user, **values})

        firestore_db().collection("users").document(current_user["id"]).update(
            {
                **values,
                "name": current_user.get("name", ""),
                "email": current_user["email"],
                "updated_at": now(),
            }
        )
        updated_profile = user()
        if not updated_profile.get("pid"):
            assign_institution_pid(
                firestore_db(),
                firestore_db().collection("users").document(current_user["id"]),
            )
        audit("update_profile", "user", current_user["id"], "Institution profile updated")
        if institution_profile_complete(user()):
            flash("Institution profile saved.", "success")
            return redirect(url_for("institution_dashboard"))
        flash("Profile saved. Complete all required details to access the workspace.", "warning")
        return redirect(url_for("profile"))
    return render_template(
        "profile.html",
        user=current_user,
        required_fields=REQUIRED_INSTITUTION_PROFILE_FIELDS,
        optional_fields=OPTIONAL_INSTITUTION_PROFILE_FIELDS,
    )


@app.route("/applications/new", methods=["GET", "POST"])
@roles("institution")
def new_application():
    current_user = user()
    form_values = {}
    existing_form_values = {}
    application_id = session.get("pending_application_id")
    if not application_id:
        application_id = firestore_db().collection("applications").document().id
        session["pending_application_id"] = application_id
    affiliation_years = range(2000, datetime.now().year + 16)
    if request.method == "POST":
        institution_name = current_user["institution_name"]
        pathway = request.form.get("pathway", "new")
        title = request.form.get("title", "").strip() or PATHWAYS.get(pathway, "AICTE Application")
        if pathway not in PATHWAYS:
            flash("Choose an application pathway.", "danger")
            return render_template(
                "new_application.html",
                user=current_user,
                state_uts=STATE_UTS,
                affiliation_years=affiliation_years,
                application_id=application_id,
                form_values=form_values,
                existing_form_values=existing_form_values,
            )
        new_institution_details = None
        existing_institution_details = None
        if pathway == "new":
            form_values = {
                field: request.form.get(field, "").strip()
                for field in NEW_INSTITUTION_FIELDS
            }
            if form_values["district"] == "Other":
                form_values["district"] = request.form.get("other_district", "").strip()
            form_values["official_email"] = form_values["official_email"].lower()
            form_values["signatory_email"] = form_values["signatory_email"].lower()

            errors = []
            required_labels = {
                "institution_name": "Institution name",
                "address": "Address & Location",
                "district": "District",
                "landline_phone": "Landline / alternate phone",
                "promoting_body_name": "Name of the promoting body",
                "registration_number": "Registration number",
                "registration_date": "Registration date",
                "signatory_name": "Authorized signatory name",
                "signatory_designation": "Signatory designation",
                "bank_name": "Bank name",
            }
            missing_labels = [
                label for field, label in required_labels.items() if not form_values[field]
            ]
            if missing_labels:
                errors.append(f"Complete the required fields: {', '.join(missing_labels)}.")
            if form_values["state"] not in STATE_UTS:
                errors.append("Choose a valid State / UT.")
            if not re.fullmatch(r"\d{6}", form_values["pin_code"]):
                errors.append("PIN code must contain exactly 6 digits.")
            if form_values["institution_type"] not in ("Trust", "Society", "Company"):
                errors.append("Choose a valid institution type.")
            if form_values["minority_status"] not in ("Yes", "No"):
                errors.append("Choose a minority status.")
            if (
                form_values["minority_status"] == "Yes"
                and form_values["minority_type"] not in ("Linguistic", "Religious")
            ):
                errors.append("Choose a minority type.")
            if form_values["minority_status"] == "No":
                form_values["minority_type"] = ""
            for field in ("official_email", "signatory_email"):
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", form_values[field]):
                    errors.append("Enter valid official and signatory email addresses.")
                    break
            website = urlparse(form_values["website_url"])
            if website.scheme not in ("http", "https") or not website.netloc:
                errors.append("Enter a valid website URL beginning with http:// or https://.")
            if not re.fullmatch(r"\d{10}", form_values["signatory_mobile"]):
                errors.append("Signatory mobile number must contain exactly 10 digits.")
            if not re.fullmatch(r"[A-Za-z0-9]{10}", form_values["trust_pan"]):
                errors.append("PAN must contain exactly 10 alphanumeric characters.")
            if not re.fullmatch(r"[A-Za-z]{4}0[A-Za-z0-9]{6}", form_values["ifsc_code"]):
                errors.append("Enter a valid 11-character IFSC code.")
            if not form_values["bank_account_number"].isdigit():
                errors.append("Bank account number must contain digits only.")
            if not re.fullmatch(r"\d+(?:\.\d+)?", form_values["land_area_acres"]):
                errors.append("Enter a valid non-negative land area.")
            if not re.fullmatch(r"\d+(?:\.\d+)?", form_values["built_up_area_sq_meters"]):
                errors.append("Enter a valid non-negative built-up area.")
            if form_values["land_ownership_type"] not in ("Lease", "Own"):
                errors.append("Choose a valid land ownership type.")
            if form_values["registration_date"]:
                try:
                    datetime.strptime(form_values["registration_date"], "%Y-%m-%d")
                except ValueError:
                    errors.append("Enter a valid registration date.")
            if errors:
                for error in errors:
                    flash(error, "danger")
                return render_template(
                    "new_application.html",
                    user=current_user,
                    state_uts=STATE_UTS,
                    affiliation_years=affiliation_years,
                    application_id=application_id,
                    form_values=form_values,
                    existing_form_values=existing_form_values,
                )
            new_institution_details = form_values
            institution_name = form_values["institution_name"]
        elif pathway == "existing":
            existing_form_values = {
                field: request.form.get(field, "").strip()
                for field in EXISTING_INSTITUTION_FIELDS
            }
            if existing_form_values["affiliating_university"] == "Other":
                existing_form_values["affiliating_university"] = request.form.get(
                    "other_affiliating_university", ""
                ).strip()
            existing_form_values["hoi_email"] = existing_form_values["hoi_email"].lower()

            errors = []
            required_labels = {
                "hoi_name": "Name of Principal / Director",
                "appointment_type": "Appointment type",
                "affiliating_university": "Affiliating University / Board",
                "affiliation_validity_year": "Affiliation validity year",
                "applied_intake_status": "Applied intake status",
                "mandatory_disclosure_url": "Mandatory disclosure web link",
                "fire_safety_expiry_date": "Fire safety certificate expiry date",
                "structural_stability_expiry_date": (
                    "Structural stability certificate expiry date"
                ),
                "anti_ragging_status": "Anti-ragging committee status",
            }
            missing_labels = [
                label
                for field, label in required_labels.items()
                if not existing_form_values[field]
            ]
            if missing_labels:
                errors.append(f"Complete the required fields: {', '.join(missing_labels)}.")
            if existing_form_values["appointment_type"] not in ("Regular", "Acting"):
                errors.append("Choose a valid appointment type.")
            if not re.fullmatch(r"\d{10}", existing_form_values["hoi_mobile"]):
                errors.append("HOI mobile number must contain exactly 10 digits.")
            if not re.fullmatch(
                r"[^@\s]+@[^@\s]+\.[^@\s]+", existing_form_values["hoi_email"]
            ):
                errors.append("Enter a valid HOI email address.")
            if not existing_form_values["affiliating_university"]:
                errors.append("Choose or enter an Affiliating University / Board.")
            if existing_form_values["affiliation_validity_year"] not in {
                str(year) for year in affiliation_years
            }:
                errors.append("Choose a valid affiliation validity year.")
            if not existing_form_values["existing_approved_intake"].isdigit():
                errors.append("Existing approved intake must be a non-negative number.")
            if existing_form_values["applied_intake_status"] not in (
                "No Change",
                "Variation",
            ):
                errors.append("Choose a valid applied intake status.")
            for field, label in (
                ("total_faculty", "Total appointed faculty"),
                ("current_student_strength", "Current enrolled students"),
            ):
                if not existing_form_values[field].isdigit():
                    errors.append(f"{label} must be a non-negative number.")
            disclosure_url = urlparse(existing_form_values["mandatory_disclosure_url"])
            if disclosure_url.scheme not in ("http", "https") or not disclosure_url.netloc:
                errors.append("Enter a valid mandatory disclosure URL.")
            for field, label in (
                ("fire_safety_expiry_date", "Fire safety certificate expiry date"),
                (
                    "structural_stability_expiry_date",
                    "Structural stability certificate expiry date",
                ),
            ):
                if existing_form_values[field]:
                    try:
                        datetime.strptime(existing_form_values[field], "%Y-%m-%d")
                    except ValueError:
                        errors.append(f"Enter a valid {label.lower()}.")
            if existing_form_values["anti_ragging_status"] not in ("Yes", "No"):
                errors.append("Choose the anti-ragging committee setup status.")
            if errors:
                for error in errors:
                    flash(error, "danger")
                return render_template(
                    "new_application.html",
                    user=current_user,
                    state_uts=STATE_UTS,
                    affiliation_years=affiliation_years,
                    application_id=application_id,
                    form_values=form_values,
                    existing_form_values=existing_form_values,
                )
            existing_form_values["academic_application_id"] = application_id
            existing_institution_details = existing_form_values
        created_at = now()
        reference = firestore_db().collection("applications").document(application_id)
        reference.set(
            {
                "user_id": current_user["id"],
                "institution_name": institution_name,
                "institution_pid": current_user["pid"],
                "institution_profile": {
                    field: current_user.get(field, "")
                    for field in (
                        *REQUIRED_INSTITUTION_PROFILE_FIELDS,
                        *OPTIONAL_INSTITUTION_PROFILE_FIELDS,
                    )
                },
                "new_institution_details": new_institution_details,
                "existing_institution_details": existing_institution_details,
                "pathway": pathway,
                "title": title,
                "status": "Draft",
                "reviewer_notes": "",
                "correction_request": "",
                "next_step": "",
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
        session.pop("pending_application_id", None)
        audit("create_application", "application", reference.id, f"Created draft for {institution_name}")
        return redirect(url_for("application_detail", app_id=reference.id))
    return render_template(
        "new_application.html",
        user=current_user,
        state_uts=STATE_UTS,
        affiliation_years=affiliation_years,
        application_id=application_id,
        form_values=form_values,
        existing_form_values=existing_form_values,
    )


@app.route("/applications/<app_id>")
@login_required
def application_detail(app_id):
    application = get_app(app_id)
    application_ref = firestore_db().collection("applications").document(app_id)
    documents = documents_for_application(application_ref, descending=True)
    for document in documents:
        storage_path = stored_document_path(app_id, document)
        document["original_file_available"] = bool(storage_path and storage_path.is_file())
    findings = collection_data_from_ref(application_ref.collection("findings"), "created_at")
    return render_template(
        "application_detail.html",
        application=application,
        documents=documents,
        findings=findings,
    )


@app.route("/applications/<app_id>/form.pdf")
@roles("admin", "reviewer")
def download_application_form_pdf(app_id):
    application = get_app(app_id)
    pdf = create_application_pdf(application)
    filename = secure_filename(f"application-{app_id}-form-details.pdf")
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/applications/<app_id>/documents/<document_id>/<action>")
@login_required
def application_document_file(app_id, document_id, action):
    if action not in ("view", "download"):
        abort(404)
    get_app(app_id)
    document_ref = (
        firestore_db()
        .collection("applications")
        .document(app_id)
        .collection("documents")
        .document(document_id)
    )
    document_snapshot = document_ref.get()
    if not document_snapshot.exists:
        abort(404)
    document = document_snapshot.to_dict() or {}
    path = stored_document_path(app_id, document)
    if not path or not path.is_file():
        abort(404, description="The original proof file is not available in storage.")
    original_name = secure_filename(document.get("original_name", "")) or "proof"
    mimetype = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    inline = action == "view" and (mimetype == "application/pdf" or mimetype.startswith("image/"))
    response = send_file(
        path,
        mimetype=mimetype,
        as_attachment=not inline,
        download_name=original_name,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    if inline:
        response.headers["Content-Security-Policy"] = "sandbox"
    return response


def collection_data_from_ref(collection, order_by=None, descending=False):
    query = collection
    if order_by:
        direction = firestore.Query.DESCENDING if descending else firestore.Query.ASCENDING
        query = query.order_by(order_by, direction=direction)
    return [{"id": doc.id, **(doc.to_dict() or {})} for doc in query.stream()]


def documents_for_application(application_ref, descending=False):
    collection = application_ref.collection("documents")
    documents = collection_data_from_ref(collection, "created_at", descending=descending)
    for document in documents:
        chunk_count = document.pop("text_chunk_count", 0)
        if chunk_count:
            chunks = collection_data_from_ref(
                collection.document(document["id"]).collection("text_chunks"),
                "position",
            )
            document["extracted_text"] = "".join(chunk["text"] for chunk in chunks)
        else:
            document.setdefault("extracted_text", "")
    return documents


@app.route("/applications/<app_id>/upload", methods=["POST"])
@login_required
def upload_document(app_id):
    current_user = user()
    if current_user["role"] != "institution":
        abort(403)
    application = get_app(app_id)
    if application["status"] not in ("Draft", "Correction Required", "Resubmitted"):
        flash("Documents cannot be changed at this workflow stage.", "warning")
        return redirect(url_for("application_detail", app_id=app_id))

    success = 0
    application_ref = firestore_db().collection("applications").document(app_id)
    documents = application_ref.collection("documents")
    for uploaded_file in request.files.getlist("files"):
        if not uploaded_file.filename:
            continue
        name = secure_filename(uploaded_file.filename)
        extension = Path(name).suffix.lower()
        contents = uploaded_file.read()
        if extension not in ALLOWED or not contents or len(contents) > MAX_FILE:
            flash(f"Skipped {name}: unsupported, empty or larger than 15 MB.", "danger")
            continue
        try:
            extracted_text = extract_file(name, contents)
            status = "Extracted" if extracted_text.strip() else "Manual review"
        except Exception as error:
            extracted_text = ""
            status = "Manual review"
            flash(f"{name}: uploaded; extraction needs manual review ({str(error)[:100]}).", "warning")
        reference = documents.document()
        text_chunks = [
            extracted_text[index:index + FIRESTORE_TEXT_CHUNK_SIZE]
            for index in range(0, len(extracted_text), FIRESTORE_TEXT_CHUNK_SIZE)
        ] if len(extracted_text.encode("utf-8")) > MAX_FIRESTORE_TEXT_BYTES else []
        document_data = {
            "application_id": app_id,
            "original_name": name,
            "size": len(contents),
            "status": status,
            "has_extracted_text": bool(extracted_text.strip()),
            "uploaded_by": session["uid"],
            "created_at": now(),
            "text_chunk_count": len(text_chunks),
        }
        storage_key = f"{reference.id}{extension}"
        storage_path = Path(app.config["UPLOAD_STORAGE_DIR"]).resolve() / app_id / storage_key
        try:
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            with storage_path.open("xb") as stored_file:
                stored_file.write(contents)
        except OSError:
            app.logger.exception("Could not save uploaded proof file for application %s", app_id)
            flash(f"{name}: the proof could not be saved to file storage.", "danger")
            continue

        saved = False
        try:
            saved_document_data = {
                **document_data,
                "storage_key": storage_key,
                "has_original_file": True,
            }
            if text_chunks:
                batch = firestore_db().batch()
                batch.set(reference, saved_document_data)
                for position, text_chunk in enumerate(text_chunks):
                    batch.set(
                        reference.collection("text_chunks").document(f"{position:06d}"),
                        {"position": position, "text": text_chunk},
                    )
                batch.commit()
            else:
                reference.set(
                    {
                        **saved_document_data,
                        "extracted_text": extracted_text,
                    }
                )
            saved = True
        finally:
            if not saved:
                storage_path.unlink(missing_ok=True)
        audit(
            "upload_document",
            "document",
            reference.id,
            f"Application {app_id}; file={name}; bytes={len(contents)}",
        )
        success += 1
    if success:
        application_ref.update(
            {
                "ai_review.status": "stale",
                "review_analysis.status": "stale",
            }
        )
        flash(f"Uploaded {success} document(s).", "success")
    return redirect(url_for("application_detail", app_id=app_id))


@app.route(
    "/applications/<app_id>/documents/<document_id>/delete",
    methods=["POST"],
)
@login_required
def delete_document(app_id, document_id):
    current_user = user()
    if current_user["role"] != "institution":
        abort(403)
    application = get_app(app_id)
    if application["status"] not in ("Draft", "Correction Required", "Resubmitted"):
        flash("Documents cannot be changed at this workflow stage.", "warning")
        return redirect(url_for("application_detail", app_id=app_id))

    document_ref = (
        firestore_db()
        .collection("applications")
        .document(app_id)
        .collection("documents")
        .document(document_id)
    )
    document_snapshot = document_ref.get()
    if not document_snapshot.exists:
        abort(404)
    document = document_snapshot.to_dict() or {}
    storage_path = stored_document_path(app_id, document)

    firestore_db().collection("applications").document(app_id).update(
        {
            "ai_review.status": "stale",
            "review_analysis.status": "stale",
        }
    )
    text_chunks = list(document_ref.collection("text_chunks").stream())
    for start in range(0, len(text_chunks), 400):
        batch = firestore_db().batch()
        for chunk in text_chunks[start:start + 400]:
            batch.delete(chunk.reference)
        batch.commit()
    document_ref.delete()

    if storage_path:
        try:
            storage_path.unlink(missing_ok=True)
        except OSError:
            app.logger.exception(
                "Could not remove uploaded proof file for application %s",
                app_id,
            )
            flash(
                "The proof record was removed, but its stored file could not be deleted.",
                "warning",
            )
        else:
            flash("Proof document deleted.", "success")
    else:
        flash("Proof document deleted.", "success")

    audit(
        "delete_document",
        "document",
        document_id,
        f"Application {app_id}; file={document.get('original_name', 'proof')}",
    )
    return redirect(url_for("application_detail", app_id=app_id))


@app.route("/applications/<app_id>/ai-review", methods=["POST"])
@roles("admin", "reviewer")
def review_application_with_ai(app_id):
    application = get_app(app_id)
    application_ref = firestore_db().collection("applications").document(app_id)
    documents = documents_for_application(application_ref)
    rule_results = evaluate(
        application["pathway"],
        [{"text": document["extracted_text"]} for document in documents],
    )
    findings = application_ref.collection("findings")
    for finding in findings.stream():
        finding.reference.delete()
    for result in rule_results:
        findings.document().set(
            {
                "application_id": app_id,
                "rule_key": result["key"],
                "issue": result["title"],
                "status": result["status"],
                "confidence": result["confidence"],
                "evidence": result["evidence"],
                "explanation": result["explanation"],
                "created_at": now(),
            }
        )

    try:
        result = run_openrouter_review(application, documents)
    except AIReviewError as error:
        app.logger.warning("AI cross-check failed for application %s: %s", app_id, error)
        checked_at = now()
        application_ref.update(
            {
                "ai_review": {
                    "status": "failed",
                    "error": str(error),
                    "checked_at": checked_at,
                    "reviewed_by": session["uid"],
                },
                "review_analysis": {
                    "status": "partial",
                    "checked_at": checked_at,
                    "rule_findings_count": sum(
                        finding["status"] == "not_detected"
                        for finding in rule_results
                    ),
                    "ai_findings_count": 0,
                    "recommendation": "",
                    "reason": (
                        "Rules-based checks completed, but the AI proof comparison "
                        f"could not finish: {error}"
                    ),
                },
                "updated_at": checked_at,
            }
        )
        audit(
            "run_combined_review",
            "application",
            app_id,
            f"Rules-based checks completed; AI review failed: {error}",
        )
        flash(
            "Rules-based checks completed, but the AI comparison did not finish. "
            f"{error}",
            "warning",
        )
        return redirect(url_for("application_detail", app_id=app_id))

    checked_at = now()
    review = {
        **result,
        "status": "completed",
        "checked_at": checked_at,
        "reviewed_by": session["uid"],
    }
    missing_rule_findings = [
        finding for finding in rule_results if finding["status"] == "not_detected"
    ]
    suggested_status = (
        "Correction Required"
        if missing_rule_findings or result["findings"]
        else "Final Review"
    )
    reasons = []
    if missing_rule_findings:
        reasons.append(f"{len(missing_rule_findings)} rules-based potential gap(s)")
    if result["findings"]:
        reasons.append(f"{len(result['findings'])} AI-identified potential issue(s)")
    recommendation_reason = (
        "; ".join(reasons)
        if reasons
        else "No potential issues were identified by the current rules or AI checks."
    )
    suggested_correction = []
    suggested_correction.extend(
        f"Confirm missing evidence for: {finding['title']}."
        for finding in missing_rule_findings
    )
    suggested_correction.extend(
        (
            f"{finding['field']} ({finding['document']}): {finding['issue']} "
            f"Suggested follow-up: {finding['suggestion']}"
        )
        for finding in result["findings"]
    )
    suggested_correction_request = "\n".join(suggested_correction)[:4000]
    suggested_next_step = (
        "Verify each flagged item against the original proof and request corrections "
        "for confirmed discrepancies."
        if suggested_status == "Correction Required"
        else "Complete manual review of the original proofs and applicable requirements "
        "before deciding the final outcome."
    )
    application_ref.update(
        {
            "ai_review": review,
            "review_analysis": {
                "status": "completed",
                "checked_at": checked_at,
                "rule_findings_count": len(missing_rule_findings),
                "ai_findings_count": len(result["findings"]),
                "recommendation": suggested_status,
                "reason": recommendation_reason,
                "suggested_correction_request": suggested_correction_request,
                "suggested_next_step": suggested_next_step,
            },
            "updated_at": checked_at,
        }
    )
    audit(
        "run_combined_review",
        "application",
        app_id,
        f"Rules-based and OpenRouter review completed; suggested status={suggested_status}.",
    )
    flash(
        f"Combined review completed. Suggested stage: {suggested_status}. "
        "Application status has not changed; an admin must save the review action.",
        "success",
    )
    return redirect(url_for("application_detail", app_id=app_id))


@app.route("/applications/<app_id>/validate", methods=["POST"])
@roles("admin", "reviewer", "institution")
def validate_application(app_id):
    application = get_app(app_id)
    application_ref = firestore_db().collection("applications").document(app_id)
    documents = documents_for_application(application_ref)
    findings = application_ref.collection("findings")
    for finding in findings.stream():
        finding.reference.delete()

    results = evaluate(
        application["pathway"],
        [{"text": document["extracted_text"]} for document in documents],
    )
    for result in results:
        findings.document().set(
            {
                "application_id": app_id,
                "rule_key": result["key"],
                "issue": result["title"],
                "status": result["status"],
                "confidence": result["confidence"],
                "evidence": result["evidence"],
                "explanation": result["explanation"],
                "created_at": now(),
            }
        )
    application_ref.update(
        {
            "ai_review.status": "stale",
            "review_analysis.status": "stale",
        }
    )
    audit("run_validation", "application", app_id, f"Generated {len(results)} heuristic findings")
    flash("Preliminary validation completed. Human review is required.", "success")
    return redirect(url_for("application_detail", app_id=app_id))


@app.route("/applications/<app_id>/submit", methods=["POST"])
@roles("institution")
def submit_application(app_id):
    application = get_app(app_id)
    application_ref = firestore_db().collection("applications").document(app_id)
    if not next(application_ref.collection("documents").limit(1).stream(), None):
        flash("Upload at least one document before submitting.", "danger")
        return redirect(url_for("application_detail", app_id=app_id))
    status = "Resubmitted" if application["status"] == "Correction Required" else "Submitted"
    application_ref.update({"status": status, "updated_at": now()})
    audit("submit_application", "application", app_id, f"Status changed to {status}")
    flash(f"Application {status.lower()}.", "success")
    return redirect(url_for("application_detail", app_id=app_id))


@app.route("/applications/<app_id>/review", methods=["POST"])
@roles("admin")
def review_application(app_id):
    application = get_app(app_id)
    status = request.form.get("status", application["status"])
    allowed = {
        "Submitted",
        "Under Review",
        "Correction Required",
        "Resubmitted",
        "Final Review",
        "Completed",
    }
    if status not in allowed:
        abort(400)
    notes = request.form.get("reviewer_notes", "").strip()
    correction = request.form.get("correction_request", "").strip()
    next_step = request.form.get("next_step", "").strip()
    firestore_db().collection("applications").document(app_id).update(
        {
            "status": status,
            "reviewer_notes": notes,
            "correction_request": correction,
            "next_step": next_step,
            "updated_at": now(),
        }
    )
    review_analysis = application.get("review_analysis") or {}
    audit(
        "review_application",
        "application",
        app_id,
        (
            f"Status={status}; AI/rules recommendation="
            f"{review_analysis.get('recommendation') or 'not available'}; "
            f"next step={next_step}; notes={notes[:250]}"
        ),
    )
    flash("Review action saved.", "success")
    return redirect(url_for("application_detail", app_id=app_id))


@app.route("/admin")
@roles("admin", "reviewer")
def admin_dashboard():
    applications = collection_data("applications", "updated_at", descending=True)
    users = collection_data("users", "created_at", descending=True)
    users_by_id = {profile["id"]: profile for profile in users}
    for application in applications:
        application["submitter_email"] = users_by_id.get(application["user_id"], {}).get("email", "")

    logs = collection_data("audit_logs", "created_at", descending=True, limit=150)
    findings = []
    documents = []
    for application in applications:
        application_ref = firestore_db().collection("applications").document(application["id"])
        for finding in collection_data_from_ref(
            application_ref.collection("findings"),
            "created_at",
            descending=True,
        ):
            findings.append(
                {
                    **finding,
                    "institution_name": application["institution_name"],
                    "title": application["title"],
                }
            )
        ai_review = application.get("ai_review") or {}
        for finding in ai_review.get("findings", []) if ai_review.get("status") == "completed" else []:
            findings.append(
                {
                    "application_id": application["id"],
                    "issue": f"AI cross-check: {finding.get('field', 'Possible mismatch')}",
                    "status": "not_detected",
                    "confidence": finding.get("confidence", 0) / 100,
                    "evidence": (
                        f"Document: {finding.get('document', 'Unidentified')}; "
                        f"Form: {finding.get('form_value', '—')}; "
                        f"Proof: {finding.get('proof_value', '—')}"
                    ),
                    "explanation": (
                        f"{finding.get('issue', '')} "
                        f"Suggestion: {finding.get('suggestion', '')}"
                    ),
                    "created_at": ai_review.get("checked_at", application.get("updated_at", "")),
                    "institution_name": application["institution_name"],
                    "title": application["title"],
                }
            )
        for document in collection_data_from_ref(
            application_ref.collection("documents"),
            "created_at",
            descending=True,
        ):
            storage_path = stored_document_path(application["id"], document)
            documents.append(
                {
                    **document,
                    "original_file_available": bool(storage_path and storage_path.is_file()),
                    "institution_name": application["institution_name"],
                    "title": application["title"],
                }
            )
    findings.sort(key=lambda item: item["created_at"], reverse=True)
    documents.sort(key=lambda item: item["created_at"], reverse=True)
    findings = findings[:150]
    documents = documents[:150]
    documents_by_application = {}
    for document in documents:
        documents_by_application.setdefault(document["application_id"], []).append(document)
    groups_by_user = {}
    for application in applications:
        user_id = application.get("user_id", "")
        group = groups_by_user.setdefault(
            user_id,
            {
                "user_id": user_id,
                "institution_name": application["institution_name"],
                "submitter_email": application["submitter_email"],
                "applications": [],
            },
        )
        application["documents"] = documents_by_application.get(application["id"], [])
        group["applications"].append(application)
    application_groups = list(groups_by_user.values())
    counts = {
        "total": len(applications),
        "pending": sum(
            application["status"] in ("Submitted", "Under Review", "Resubmitted", "Final Review")
            for application in applications
        ),
        "flagged": sum(finding["status"] == "not_detected" for finding in findings),
        "completed": sum(application["status"] == "Completed" for application in applications),
    }
    return render_template(
        "admin.html",
        applications=applications,
        users=users,
        logs=logs,
        findings=findings,
        docs=documents,
        application_groups=application_groups,
        counts=counts,
    )


@app.route("/admin/users/<uid>/role", methods=["POST"])
@roles("admin")
def update_role(uid):
    role = request.form.get("role", "institution")
    if role not in ("institution", "reviewer", "admin"):
        abort(400)
    if uid == session["uid"] and role != "admin":
        flash("You cannot remove your own admin role.", "danger")
        return redirect(url_for("admin_dashboard"))
    profile_ref = firestore_db().collection("users").document(uid)
    if not profile_ref.get().exists:
        abort(404)
    profile_ref.update({"role": role})
    audit("change_user_role", "user", uid, f"Role changed to {role}")
    flash("User role updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<uid>/active", methods=["POST"])
@roles("admin")
def toggle_user(uid):
    if uid == session["uid"]:
        flash("You cannot disable your own account.", "danger")
    else:
        profile_ref = firestore_db().collection("users").document(uid)
        profile_doc = profile_ref.get()
        if profile_doc.exists:
            profile_ref.update({"active": not (profile_doc.to_dict() or {}).get("active", True)})
            audit("toggle_user", "user", uid, "Account active state toggled")
            flash("Account state updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/export.csv")
@roles("admin", "reviewer")
def export_csv():
    applications = collection_data("applications", "created_at", descending=True)
    users_by_id = {profile["id"]: profile for profile in collection_data("users")}
    rows = [
        [
            application["id"],
            application["institution_name"],
            application["title"],
            application["pathway"],
            application["status"],
            users_by_id.get(application["user_id"], {}).get("email", ""),
            application["created_at"],
            application["updated_at"],
            application.get("reviewer_notes", ""),
            application.get("correction_request", ""),
            application.get("next_step", ""),
        ]
        for application in applications
    ]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Application ID",
            "Institution",
            "Title",
            "Pathway",
            "Status",
            "Submitter",
            "Created",
            "Updated",
            "Reviewer Notes",
            "Correction Request",
            "Next Step",
        ]
    )
    writer.writerows(rows)
    memory_file = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    memory_file.seek(0)
    audit("export_applications", "report", None, f"Exported {len(rows)} rows")
    return send_file(
        memory_file,
        mimetype="text/csv",
        as_attachment=True,
        download_name="veriaicte_applications.csv",
    )


@app.route("/health")
def health():
    return jsonify(status="ok", app="VeriAICTE")


@app.errorhandler(403)
def forbidden(error):
    return render_template(
        "message.html",
        title="Access denied",
        message="Your account does not have permission to access this page.",
    ), 403


@app.errorhandler(FirebaseSetupError)
def firebase_setup_error(error):
    return render_template(
        "message.html",
        title="Firebase setup required",
        message=str(error),
    ), 503


@app.errorhandler(413)
def too_large(error):
    return render_template(
        "message.html",
        title="Upload too large",
        message="Request exceeds the 40 MB total upload limit.",
    ), 413


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
