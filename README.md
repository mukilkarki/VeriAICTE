# VeriAICTE — Full-stack starter

A Flask application for institution submissions and compliance reviewers. Firebase Authentication handles email/password sign-in, and Cloud Firestore stores user profiles, applications, extracted document text, findings and audit logs.

## Included panels

**Institution user panel:** register/login; institution profile and history; create draft applications, including pathway-specific New Institution Approval and Existing Institution / EoA forms with profile autofill; upload and remove proof files while the application is a draft, correction required, or resubmitted; preliminary validation report; correction/replacement workflow; submit/resubmit and status tracking.

**Admin/reviewer panel:** overview counts; institution-wise queue organized into per-user application folders; downloadable PDF snapshots of each application's profile and submitted form; secure view/download links for retained proof files; OCR extraction status; combined rules-based and AI proof cross-checks with suggested workflow stages; correction request, reviewer notes and recommended next step; user role management (admin-only); audit logs; CSV export. Admins and reviewers can inspect proofs but cannot upload or delete them. Suggested stages never change an application automatically; only an administrator can save or override a stage.

Workflow: Draft → Submitted → Under Review → Correction Required → Resubmitted → Final Review → Completed.

## Run locally

Python 3.10+ recommended.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# If .env does not already exist, copy .env.example to .env.
# Add the Firebase web app settings and credentials described below to .env.
python -m app.main
```

Open http://127.0.0.1:5000. For image and scanned-PDF OCR install Tesseract OCR and ensure its executable is on PATH. Text-based PDFs are supported directly; scanned PDF pages are rendered for OCR. Pillow-supported raster image formats (including JPG, PNG, BMP, GIF, TIFF, WebP and AVIF) are supported (up to 40 PDF pages or image frames per proof). Original uploads are stored under `instance/uploads`; keep this directory private and backed up.

## Firebase setup

1. In Firebase Console, create or select a project, enable **Authentication → Sign-in method → Email/Password**, and create a **Cloud Firestore** database.
2. Register a Firebase web app and copy its API key, Auth domain, project ID and app ID into `.env` as `FIREBASE_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID` and `FIREBASE_APP_ID`. Add your deployed hostname to Firebase Authentication's authorized domains when using a non-local URL.
3. Give the Flask server Firebase Admin credentials. Locally, use a service-account JSON file with `GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json` or set `FIREBASE_SERVICE_ACCOUNT_JSON` to its minified JSON contents. In Docker, set `FIREBASE_SERVICE_ACCOUNT_JSON` in the ignored `.env` file. In production, prefer the hosting platform's Application Default Credentials/workload identity instead of a downloaded key.
4. The server uses the Admin SDK, so Firestore client access can be denied by default. For example, deploy rules that deny all direct client reads and writes; the Flask server's Admin SDK bypasses client security rules:

```javascript
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /{document=**} {
      allow read, write: if false;
    }
  }
}
```

## Cloudflare Turnstile

The login and registration forms use Cloudflare Turnstile (Cloudflare's CAPTCHA alternative). Create a Turnstile widget in the Cloudflare dashboard, add the app's public hostname to its allowed hostnames, and copy its site key and secret key into `.env` as `CLOUDFLARE_TURNSTILE_SITE_KEY` and `CLOUDFLARE_TURNSTILE_SECRET_KEY`. Turnstile's client widget must be completed before the submit button is enabled. The Flask server verifies each one-use response token with Cloudflare Siteverify, including the expected `LOGIN` or `REGISTER` action, before it accepts Firebase sign-in. Keep the secret key server-side; never put it in browser code. Both keys are required, including when running with Docker Compose.

Set `ADMIN_EMAIL` to an existing Firebase Authentication email to grant it the administrator role. If the account does not exist yet, also set `ADMIN_PASSWORD` and the app will provision it at first connection. Keep that password and all service-account credentials private. New self-registered accounts receive the institution role; administrators can promote reviewers and other administrators from the Admin Panel.

This integration starts with a fresh Firestore database; existing SQLite records are not imported.

New institution accounts must complete the institution profile before they can use the workspace. The profile stores the institution identity, contact, sponsoring body, institution head, affiliation, public disclosures and optional academic information in that user's Firestore `users` document. A Firestore transaction assigns each user one permanent `AICTE/ID/{year}/{sequence}` PID; the sequence starts at `001` each year. New applications copy the saved profile details into their Firestore record and use the saved institution name and PID.

## Optional AI cross-check

Set `OPENROUTER_API_KEY` in `.env` to enable the reviewer-only **Run AI cross-check** action on an application. `OPENROUTER_MODEL` defaults to `openrouter/free`, which routes to an available free model when OpenRouter has one; model availability and limits can change. A different OpenRouter model can be selected with that setting. The key stays on the server and is not exposed to browsers.

The review runs only when an administrator or reviewer triggers it. It sends selected application/profile fields and bounded excerpts of extracted proof text to OpenRouter. Bank details and common PAN/Aadhaar/account-number patterns are excluded or redacted, but other personal information in proofs may still be sent. Review the provider's data-use terms and obtain any required institutional consent before enabling this feature. The result is a triage aid, not proof authentication, legal/compliance verification, or a decision; reviewers must inspect the original uploaded document. New uploads mark an earlier AI review as stale.

## Docker

If `.env` does not already exist, copy `.env.example` to `.env`. Set the Firebase web app values, a strong Flask `SECRET_KEY`, and Firebase Admin credentials, then run:

```bash
docker compose up --build
```

Open http://localhost:5000. Docker Compose passes Firebase configuration to the app and persists original proof files in `./instance/uploads`. Do not commit `.env`, uploaded proof files, or service-account credentials.

## Important limitations / production checklist

- The checklist is keyword heuristic matching, not an LLM, legal opinion, compliance certificate or official AICTE determination. AI cross-check findings can be wrong or miss issues; confidence scores are model-reported and uncalibrated.
- Newly uploaded original file bytes are stored on the server in a private application-specific directory and exposed only through authenticated view/download routes. Files uploaded before original-file storage was added have no retained bytes and must be re-uploaded to enable viewing/downloading. Extracted text and metadata are stored separately. When manually triggered, bounded extracted-text excerpts and selected application/profile fields are sent to OpenRouter.
- This MVP does not authenticate documents, verify signatures/QR codes, connect to AICTE/NSWS, or automatically validate legal status.
- Before real deployment add CSRF protection, rate limiting, password reset, MFA for privileged users, secure cookie flags, session expiry, HTTPS, malware scanning, file-signature/MIME validation, encrypted storage, retention/deletion controls, backups, privacy and security review, and tenant-isolation testing.
- Validate every checklist item against the current official AICTE Approval Process Handbook, amendments, programme-specific requirements, state/local rules, and qualified legal guidance.
