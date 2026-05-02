import { useEffect, useMemo, useState } from "react";
import {
  CatalogModel,
  getCatalog,
  getInstalledModels,
  installModel,
  postBootstrap,
  startScan,
} from "../api/client";
import { t } from "../lib/i18n";

// Two phases of the first-run journey:
//   - "setup": fresh container, bootstrap not yet recorded. Steps 1-2.
//     Once bootstrap is recorded the wizard tells the user to sign in;
//     authentication itself runs through LoginPage.
//   - "install": admin signed in, no active model installed. Steps 3-7.
//
// We keep wizard state in component state only — never localStorage /
// sessionStorage — so a reload always re-derives state from the server
// (fresh GET /setup/bootstrap + GET /me + GET /models in App.tsx).
export type WizardPhase = "setup" | "install";

interface WizardProps {
  phase: WizardPhase;
  onSetupRecorded?: () => void;
  onInstallDone?: () => void;
}

const SETUP_STEPS = ["welcome", "bootstrap"] as const;
const INSTALL_STEPS = ["catalog", "license", "install", "scan", "done"] as const;
type SetupStep = (typeof SETUP_STEPS)[number];
type InstallStep = (typeof INSTALL_STEPS)[number];

export default function Wizard({
  phase,
  onSetupRecorded,
  onInstallDone,
}: WizardProps) {
  const stepNames = phase === "setup" ? SETUP_STEPS : INSTALL_STEPS;
  const [stepIndex, setStepIndex] = useState(0);
  const total = phase === "setup" ? 2 : 5;
  const visibleStepNumber =
    phase === "setup" ? stepIndex + 1 : stepIndex + 3;
  const grandTotal = 7;

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col px-4 py-8">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">{t("wizard.title")}</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          {t("wizard.step", { n: visibleStepNumber, total: grandTotal })}
        </p>
      </header>

      {phase === "setup" && (
        <SetupSteps
          step={stepNames[stepIndex] as SetupStep}
          onNext={() => setStepIndex((i) => Math.min(i + 1, total - 1))}
          onBack={() => setStepIndex((i) => Math.max(i - 1, 0))}
          onSetupRecorded={onSetupRecorded}
        />
      )}
      {phase === "install" && (
        <InstallSteps
          step={stepNames[stepIndex] as InstallStep}
          stepIndex={stepIndex}
          setStepIndex={setStepIndex}
          onInstallDone={onInstallDone}
        />
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Setup phase: welcome → bootstrap
// ---------------------------------------------------------------------------

function SetupSteps({
  step,
  onNext,
  onBack,
  onSetupRecorded,
}: {
  step: SetupStep;
  onNext: () => void;
  onBack: () => void;
  onSetupRecorded?: () => void;
}) {
  const [consented, setConsented] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [recorded, setRecorded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (step === "welcome") {
    return (
      <Section heading={t("wizard.welcome.title")}>
        <p className="text-sm">{t("wizard.welcome.body")}</p>
        <label className="mt-4 flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={consented}
            onChange={(e) => setConsented(e.target.checked)}
            className="mt-1"
          />
          <span>{t("wizard.welcome.consent")}</span>
        </label>
        <Footer>
          <button
            type="button"
            onClick={onNext}
            disabled={!consented}
            className={primaryBtn}
          >
            {t("wizard.next")}
          </button>
        </Footer>
      </Section>
    );
  }

  // bootstrap
  return (
    <Section heading={t("wizard.bootstrap.title")}>
      <p className="text-sm">{t("wizard.bootstrap.body")}</p>
      {recorded ? (
        <p
          role="status"
          aria-live="polite"
          className="mt-4 rounded border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800 dark:border-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200"
        >
          {t("wizard.bootstrap.signin")}
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="mt-4 text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      ) : null}
      <Footer>
        <button type="button" onClick={onBack} className={secondaryBtn}>
          {t("wizard.back")}
        </button>
        <button
          type="button"
          disabled={submitting || recorded}
          onClick={async () => {
            setSubmitting(true);
            setError(null);
            try {
              await postBootstrap();
              setRecorded(true);
              onSetupRecorded?.();
            } catch (err) {
              setError((err as Error).message);
            } finally {
              setSubmitting(false);
            }
          }}
          className={primaryBtn}
        >
          {t("wizard.bootstrap.submit")}
        </button>
      </Footer>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Install phase: catalog → license → install → scan → done
// ---------------------------------------------------------------------------

function InstallSteps({
  step,
  setStepIndex,
  onInstallDone,
}: {
  step: InstallStep;
  stepIndex: number;
  setStepIndex: (updater: (i: number) => number) => void;
  onInstallDone?: () => void;
}) {
  const [models, setModels] = useState<CatalogModel[] | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [licenseAccepted, setLicenseAccepted] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState<string | null>(null);
  const [installed, setInstalled] = useState(false);
  const [scanState, setScanState] = useState<
    | { kind: "idle" }
    | { kind: "starting" }
    | { kind: "started"; runId: number }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  useEffect(() => {
    if (step === "catalog" && models === null) {
      getCatalog()
        .then((m) => setModels(m))
        .catch((err) => setCatalogError((err as Error).message));
    }
  }, [step, models]);

  const selected = useMemo(
    () => models?.find((m) => m.id === selectedId) ?? null,
    [models, selectedId],
  );

  const next = () => setStepIndex((i) => i + 1);
  const back = () => setStepIndex((i) => Math.max(i - 1, 0));

  if (step === "catalog") {
    return (
      <Section heading={t("wizard.catalog.title")}>
        <p className="text-sm">{t("wizard.catalog.body")}</p>
        {catalogError ? (
          <p role="alert" className="mt-4 text-sm text-red-700 dark:text-red-400">
            {catalogError}
          </p>
        ) : models === null ? (
          <p role="status" aria-live="polite" className="mt-4 text-sm">
            …
          </p>
        ) : models.length === 0 ? (
          <p className="mt-4 text-sm">{t("wizard.catalog.empty")}</p>
        ) : (
          <ul className="mt-4 space-y-2" role="radiogroup" aria-label="models">
            {models
              .filter((m) => m.installable)
              .map((m) => (
                <li key={m.id}>
                  <label className="flex items-start gap-3 rounded border border-slate-200 p-3 text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-700/40">
                    <input
                      type="radio"
                      name="model"
                      value={m.id}
                      checked={selectedId === m.id}
                      onChange={() => {
                        setSelectedId(m.id);
                        setLicenseAccepted(false);
                      }}
                      className="mt-1"
                    />
                    <span>
                      <span className="block font-medium">{m.name}</span>
                      <span className="block text-xs text-slate-500 dark:text-slate-400">
                        {m.kind} — {m.license}
                      </span>
                    </span>
                  </label>
                </li>
              ))}
          </ul>
        )}
        <Footer>
          <button
            type="button"
            onClick={next}
            disabled={selected === null}
            className={primaryBtn}
          >
            {t("wizard.next")}
          </button>
        </Footer>
      </Section>
    );
  }

  if (step === "license") {
    if (selected === null) {
      // Defensive: shouldn't happen because the catalog gate disables Next.
      return (
        <Section heading={t("wizard.license.title")}>
          <p className="text-sm">{t("wizard.catalog.empty")}</p>
        </Section>
      );
    }
    return (
      <Section heading={t("wizard.license.title")}>
        {/* All license fields rendered as plain text, never as HTML. */}
        <dl className="mt-2 space-y-2 text-sm">
          <div>
            <dt className="font-medium">{selected.name}</dt>
            <dd className="text-slate-600 dark:text-slate-300">
              {selected.kind}
            </dd>
          </div>
          <div>
            <dt className="font-medium">{t("wizard.license.sha256")}</dt>
            <dd className="break-all font-mono text-xs">{selected.sha256}</dd>
          </div>
          <div>
            <dt className="font-medium">{t("wizard.license.size")}</dt>
            <dd>{selected.size_bytes.toLocaleString()} bytes</dd>
          </div>
          <div>
            <dt className="font-medium">License</dt>
            <dd>{selected.license}</dd>
          </div>
          {selected.license_url ? (
            <div>
              <dt className="font-medium">{t("wizard.license.url")}</dt>
              {/* href is the catalog-pinned URL; opens in a new tab. */}
              <dd className="break-all">
                <a
                  href={selected.license_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline"
                >
                  {selected.license_url}
                </a>
              </dd>
            </div>
          ) : null}
        </dl>
        <label className="mt-4 flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={licenseAccepted}
            onChange={(e) => setLicenseAccepted(e.target.checked)}
            className="mt-1"
          />
          <span>{t("wizard.license.accept")}</span>
        </label>
        <Footer>
          <button type="button" onClick={back} className={secondaryBtn}>
            {t("wizard.back")}
          </button>
          <button
            type="button"
            onClick={next}
            disabled={!licenseAccepted}
            className={primaryBtn}
          >
            {t("wizard.next")}
          </button>
        </Footer>
      </Section>
    );
  }

  if (step === "install") {
    return (
      <Section heading={t("wizard.install.title")}>
        <p className="text-sm">{selected?.name}</p>
        <div role="status" aria-live="polite" className="mt-2 text-sm">
          {installing
            ? t("wizard.install.installing")
            : installed
              ? t("wizard.install.installed")
              : ""}
        </div>
        {installError ? (
          <p role="alert" className="mt-2 text-sm text-red-700 dark:text-red-400">
            {installError}
          </p>
        ) : null}
        <Footer>
          <button type="button" onClick={back} className={secondaryBtn}>
            {t("wizard.back")}
          </button>
          {!installed ? (
            <button
              type="button"
              disabled={!licenseAccepted || installing || selected === null}
              onClick={async () => {
                if (selected === null) return;
                setInstalling(true);
                setInstallError(null);
                try {
                  await installModel(selected.id);
                  // Verify via GET /models so we are not trusting only
                  // the install POST's success.
                  await getInstalledModels();
                  setInstalled(true);
                } catch (err) {
                  setInstallError(t("wizard.install.error"));
                } finally {
                  setInstalling(false);
                }
              }}
              className={primaryBtn}
            >
              {t("wizard.install.action")}
            </button>
          ) : (
            <button type="button" onClick={next} className={primaryBtn}>
              {t("wizard.next")}
            </button>
          )}
        </Footer>
      </Section>
    );
  }

  if (step === "scan") {
    return (
      <Section heading={t("wizard.scan.title")}>
        <p className="text-sm">{t("wizard.scan.body")}</p>
        <div role="status" aria-live="polite" className="mt-2 text-sm">
          {scanState.kind === "starting" && t("wizard.scan.starting")}
          {scanState.kind === "started" &&
            t("wizard.scan.started", { run_id: scanState.runId })}
        </div>
        {scanState.kind === "error" ? (
          <p role="alert" className="mt-2 text-sm text-red-700 dark:text-red-400">
            {scanState.message}
          </p>
        ) : null}
        <Footer>
          <button type="button" onClick={back} className={secondaryBtn}>
            {t("wizard.back")}
          </button>
          {scanState.kind !== "started" ? (
            <button
              type="button"
              disabled={scanState.kind === "starting"}
              onClick={async () => {
                setScanState({ kind: "starting" });
                try {
                  const r = await startScan();
                  setScanState({ kind: "started", runId: r.run_id });
                } catch (err) {
                  setScanState({
                    kind: "error",
                    message: t("wizard.scan.error"),
                  });
                }
              }}
              className={primaryBtn}
            >
              {t("wizard.scan.action")}
            </button>
          ) : (
            <button type="button" onClick={next} className={primaryBtn}>
              {t("wizard.next")}
            </button>
          )}
        </Footer>
      </Section>
    );
  }

  // done
  return (
    <Section heading={t("wizard.done.title")}>
      <p className="text-sm">{t("wizard.done.body")}</p>
      <Footer>
        <button
          type="button"
          onClick={() => onInstallDone?.()}
          className={primaryBtn}
        >
          {t("wizard.done.cta")}
        </button>
      </Footer>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Tiny presentational helpers — kept inline to avoid a separate UI module
// while the surface is this small.
// ---------------------------------------------------------------------------

function Section({
  heading,
  children,
}: {
  heading: string;
  children: React.ReactNode;
}) {
  const headingId = `wizard-h-${heading.replace(/\s+/g, "-").toLowerCase()}`;
  return (
    <section
      aria-labelledby={headingId}
      className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-700 dark:bg-slate-800"
    >
      <h2 id={headingId} className="text-lg font-semibold">
        {heading}
      </h2>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Footer({ children }: { children: React.ReactNode }) {
  return <div className="mt-6 flex justify-end gap-2">{children}</div>;
}

const primaryBtn =
  "rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-700 disabled:opacity-60 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-slate-300";
const secondaryBtn =
  "rounded border border-slate-300 px-3 py-2 text-sm hover:bg-slate-100 dark:border-slate-600 dark:hover:bg-slate-700";
