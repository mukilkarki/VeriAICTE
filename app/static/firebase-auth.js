import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import {
  createUserWithEmailAndPassword,
  getAuth,
  signInWithEmailAndPassword,
  signOut,
  updateProfile,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";

const form = document.querySelector("#auth-form");
const errorBox = document.querySelector("#auth-error");
const turnstileStatus = document.querySelector("#turnstile-status");
const turnstileWidget = document.querySelector("#turnstile-widget");
const config = window.firebaseConfig;
const submitButton = form?.querySelector("button");

if (form && errorBox) {
  if (submitButton) {
    submitButton.disabled = true;
  }
  window.onTurnstileSuccess = () => {
    if (submitButton) {
      submitButton.disabled = false;
    }
    if (turnstileStatus) {
      turnstileStatus.textContent = "Security check complete.";
    }
  };
  window.onTurnstileExpired = () => {
    if (submitButton) {
      submitButton.disabled = true;
    }
    if (turnstileStatus) {
      turnstileStatus.textContent = "Security check expired. Complete it again to continue.";
    }
  };
  window.onTurnstileError = () => {
    if (submitButton) {
      submitButton.disabled = true;
    }
    if (turnstileStatus) {
      turnstileStatus.textContent = "Security check failed to load. Refresh the page and try again.";
    }
  };
  const turnstileScript = document.querySelector("#cloudflare-turnstile-api");
  if (turnstileScript && turnstileWidget) {
    const renderTurnstile = () => {
      if (!window.turnstile || turnstileWidget.dataset.widgetId) {
        return;
      }
      turnstileWidget.dataset.widgetId = window.turnstile.render(turnstileWidget, {
        sitekey: turnstileWidget.dataset.sitekey,
        action: form.dataset.mode.toUpperCase(),
        callback: window.onTurnstileSuccess,
        "expired-callback": window.onTurnstileExpired,
        "error-callback": window.onTurnstileError,
      });
    };
    turnstileScript.addEventListener("load", renderTurnstile);
    turnstileScript.addEventListener("error", window.onTurnstileError);
    renderTurnstile();
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.hidden = true;

    if (!config?.apiKey || !config?.authDomain || !config?.projectId || !config?.appId) {
      errorBox.textContent = "Firebase web settings are missing. Configure FIREBASE_API_KEY, FIREBASE_AUTH_DOMAIN, FIREBASE_PROJECT_ID and FIREBASE_APP_ID.";
      errorBox.hidden = false;
      return;
    }

    const formData = new FormData(form);
    const turnstileToken = String(formData.get("cf-turnstile-response") || "");
    if (!turnstileToken) {
      errorBox.textContent = "Complete the Cloudflare security challenge before continuing.";
      errorBox.hidden = false;
      return;
    }
    submitButton.disabled = true;
    const email = String(formData.get("email") || "").trim();
    const password = String(formData.get("password") || "");
    const mode = form.dataset.mode;
    const name = String(formData.get("name") || "").trim();
    let auth;

    try {
      const challengeResponse = await fetch("/auth/turnstile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ mode, turnstileToken }),
      });
      const challengeResult = await challengeResponse.json();
      if (!challengeResponse.ok) {
        throw new Error(challengeResult.error || "Unable to verify the security challenge.");
      }

      auth = getAuth(initializeApp(config));
      const credential = mode === "register"
        ? await createUserWithEmailAndPassword(auth, email, password)
        : await signInWithEmailAndPassword(auth, email, password);

      if (mode === "register" && name) {
        await updateProfile(credential.user, { displayName: name });
      }

      const response = await fetch("/auth/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          idToken: await credential.user.getIdToken(),
          mode,
          name,
        }),
      });
      let result;
      try {
        result = await response.json();
      } catch {
        throw new Error("The server could not verify Firebase sign-in. Check Firebase Admin credentials and server logs.");
      }
      if (!response.ok) {
        throw new Error(result.error || "Unable to complete sign-in.");
      }
      window.location.assign(result.redirect);
    } catch (error) {
      if (auth?.currentUser) {
        await signOut(auth);
      }
      errorBox.textContent = firebaseErrorMessage(error);
      errorBox.hidden = false;
    } finally {
      if (window.turnstile) {
        window.turnstile.reset();
      }
    }
  });
}

function firebaseErrorMessage(error) {
  const messages = {
    "auth/email-already-in-use": "An account already exists for this email. Sign in instead.",
    "auth/invalid-credential": "Invalid email or password.",
    "auth/invalid-email": "Enter a valid email address.",
    "auth/network-request-failed": "Could not connect to Firebase. Check your connection and try again.",
    "auth/too-many-requests": "Too many attempts. Wait a little and try again.",
    "auth/weak-password": "Choose a password with at least 6 characters.",
    "auth/user-disabled": "This Firebase account is disabled. Contact an administrator.",
    "auth/user-not-found": "Invalid email or password.",
    "auth/wrong-password": "Invalid email or password.",
  };
  return messages[error.code] || error.message || "Unable to sign in. Check your Firebase configuration and try again.";
}
