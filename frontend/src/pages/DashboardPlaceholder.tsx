import { t } from "../lib/i18n";

export default function DashboardPlaceholder() {
  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <section
        aria-labelledby="dashboard-title"
        className="max-w-md rounded-lg border border-slate-200 bg-white p-6 text-center shadow-sm dark:border-slate-700 dark:bg-slate-800"
      >
        <h1 id="dashboard-title" className="text-xl font-semibold">
          {t("dashboard.placeholder.title")}
        </h1>
        <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
          {t("dashboard.placeholder.body")}
        </p>
      </section>
    </main>
  );
}
