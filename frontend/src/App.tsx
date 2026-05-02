import { useCallback, useEffect, useState } from "react";
import {
  BootstrapStatus,
  InstalledModel,
  MeResponse,
  getBootstrap,
  getInstalledModels,
  getMe,
} from "./api/client";
import LoginPage from "./pages/LoginPage";
import Wizard from "./pages/Wizard";
import DashboardPlaceholder from "./pages/DashboardPlaceholder";

// Routing shell — purely a function of three server-derived facts:
//   - bootstrap.ready: terms recorded AND an admin user exists
//   - me:              authed user, or null
//   - models:          at least one model registry row, with present_on_disk
//
// Order:
//   bootstrap not ready  → Wizard(phase="setup")
//   not authed           → LoginPage
//   authed, no model     → Wizard(phase="install")
//   authed, model exists → DashboardPlaceholder
//
// We never persist this state — every reload re-fetches.

type Snapshot = {
  bootstrap: BootstrapStatus;
  me: MeResponse | null;
  models: InstalledModel[];
};

export default function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const bootstrap = await getBootstrap();
      let me: MeResponse | null = null;
      let models: InstalledModel[] = [];
      if (bootstrap.ready) {
        me = await getMe();
        if (me !== null) {
          models = await getInstalledModels();
        }
      }
      setSnapshot({ bootstrap, me, models });
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (error !== null && snapshot === null) {
    return (
      <main className="flex min-h-screen items-center justify-center px-4">
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      </main>
    );
  }

  if (snapshot === null) {
    return (
      <main className="flex min-h-screen items-center justify-center px-4">
        <p role="status" aria-live="polite" className="text-sm">
          …
        </p>
      </main>
    );
  }

  if (!snapshot.bootstrap.ready) {
    return <Wizard phase="setup" onSetupRecorded={refresh} />;
  }
  if (snapshot.me === null) {
    return <LoginPage />;
  }
  if (snapshot.models.length === 0) {
    return <Wizard phase="install" onInstallDone={refresh} />;
  }
  return <DashboardPlaceholder />;
}
