import { FormEvent, useState } from "react";
import { ApiError, login } from "../api/client";
import { t } from "../lib/i18n";

type Status =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "error"; message: string }
  | { kind: "success" };

export default function LoginPage() {
  const [immichUrl, setImmichUrl] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<Status>({ kind: "idle" });

  const submitting = status.kind === "submitting";

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setStatus({ kind: "submitting" });
    try {
      await login({
        immich_url: immichUrl.trim(),
        username: username.trim(),
        password,
      });
      // Wipe the password from component state immediately on success
      // so it does not linger in React's fiber tree any longer than
      // strictly necessary.
      setPassword("");
      setStatus({ kind: "success" });
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.code === "invalid"
            ? t("login.error.invalid")
            : err.code === "unreachable"
              ? t("login.error.unreachable")
              : t("login.error.generic")
          : t("login.error.generic");
      setStatus({ kind: "error", message });
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-700 dark:bg-slate-800"
        aria-labelledby="login-title"
      >
        <h1 id="login-title" className="text-xl font-semibold">
          {t("login.title")}
        </h1>

        <label className="block text-sm">
          <span className="mb-1 block">{t("login.immich_url")}</span>
          <input
            type="url"
            required
            autoComplete="url"
            value={immichUrl}
            onChange={(e) => setImmichUrl(e.target.value)}
            placeholder="https://immich.example.com"
            className="w-full rounded border border-slate-300 bg-white px-3 py-2 dark:border-slate-600 dark:bg-slate-900"
          />
        </label>

        <label className="block text-sm">
          <span className="mb-1 block">{t("login.username")}</span>
          <input
            type="text"
            required
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="w-full rounded border border-slate-300 bg-white px-3 py-2 dark:border-slate-600 dark:bg-slate-900"
          />
        </label>

        <label className="block text-sm">
          <span className="mb-1 block">{t("login.password")}</span>
          <input
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded border border-slate-300 bg-white px-3 py-2 dark:border-slate-600 dark:bg-slate-900"
          />
        </label>

        {status.kind === "error" && (
          <div role="alert" className="text-sm text-red-700 dark:text-red-400">
            {status.message}
          </div>
        )}

        {status.kind === "success" && (
          <div role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
            {t("login.success")}
          </div>
        )}

        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded bg-slate-900 px-3 py-2 text-white hover:bg-slate-700 disabled:opacity-60 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-slate-300"
        >
          {submitting ? t("login.submitting") : t("login.submit")}
        </button>

        <p className="text-xs text-slate-500 dark:text-slate-400">
          {t("login.forgot")}
        </p>
      </form>
    </main>
  );
}
