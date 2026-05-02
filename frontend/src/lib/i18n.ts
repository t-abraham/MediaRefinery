// English-only at v2.0; every user-visible string flows through t()
// so future locales are mechanical work. Keys are dotted and checked
// at lookup time — missing keys fall back to the key itself rather
// than throwing.
const en: Record<string, string> = {
  "login.title": "Sign in to MediaRefinery",
  "login.immich_url": "Immich URL",
  "login.username": "Username or email",
  "login.password": "Password",
  "login.submit": "Sign in",
  "login.submitting": "Signing in…",
  "login.forgot": "Forgot your password? Reset it in Immich.",
  "login.error.invalid": "Invalid Immich URL, username, or password.",
  "login.error.unreachable": "Could not reach the MediaRefinery service.",
  "login.error.generic": "Sign-in failed. Please try again.",
  "login.success": "Signed in.",

  "wizard.title": "Set up MediaRefinery",
  "wizard.step": "Step {n} of {total}",
  "wizard.next": "Continue",
  "wizard.back": "Back",
  "wizard.finish": "Finish",
  "wizard.welcome.title": "Welcome",
  "wizard.welcome.body":
    "MediaRefinery sorts your Immich library into categories you control. " +
    "It runs entirely on your server. Classifier models are downloaded on demand and you accept each licence before install.",
  "wizard.welcome.consent": "I have read the above and accept the project terms.",
  "wizard.bootstrap.title": "Record your acceptance",
  "wizard.bootstrap.body":
    "Recording acceptance creates the first-time configuration on this server. This is a one-time step and cannot be undone anonymously.",
  "wizard.bootstrap.submit": "Record and continue",
  "wizard.bootstrap.signin":
    "Acceptance recorded. Sign in with your Immich credentials to continue setup — the first user to sign in becomes the local administrator.",
  "wizard.catalog.title": "Choose a classifier model",
  "wizard.catalog.body":
    "Pick the model you want to install. The catalog is curated and pinned by SHA256; nothing else is downloaded.",
  "wizard.catalog.empty": "No installable models in the catalog.",
  "wizard.license.title": "Review licence",
  "wizard.license.sha256": "Pinned SHA256",
  "wizard.license.size": "Download size",
  "wizard.license.url": "Licence text",
  "wizard.license.accept": "I accept this model's licence and authorise install.",
  "wizard.install.title": "Install model",
  "wizard.install.action": "Install",
  "wizard.install.installing": "Installing — this can take a few minutes…",
  "wizard.install.installed": "Model installed.",
  "wizard.install.error": "Install failed. You can retry from the dashboard.",
  "wizard.scan.title": "Run a first scan",
  "wizard.scan.body":
    "The first scan runs in dry-run mode and writes nothing to Immich. You can review results before enabling real actions.",
  "wizard.scan.action": "Start dry-run scan",
  "wizard.scan.starting": "Starting…",
  "wizard.scan.started": "Scan started — run #{run_id}.",
  "wizard.scan.error": "Could not start the scan.",
  "wizard.done.title": "All set",
  "wizard.done.body":
    "MediaRefinery is configured. Open the dashboard to review your scan and refine your categories.",
  "wizard.done.cta": "Open dashboard",

  "dashboard.placeholder.title": "Dashboard",
  "dashboard.placeholder.body": "PR 3 lands here.",
};

export function t(key: string, vars?: Record<string, string | number>): string {
  const template = en[key] ?? key;
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (_, name: string) =>
    vars[name] === undefined ? `{${name}}` : String(vars[name]),
  );
}
