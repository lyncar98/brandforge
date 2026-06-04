import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import AssetsPage from "./components/AssetsPage";
import ApplyPage from "./components/ApplyPage";
import PlanPage from "./components/PlanPage";
import SpecPage from "./components/SpecPage";
import "./globals.css";

const qc = new QueryClient();

type Tab = "assets" | "apply" | "plan" | "spec";

const TABS: { id: Tab; label: string }[] = [
  { id: "assets", label: "Assets" },
  { id: "apply", label: "Apply" },
  { id: "plan", label: "Plan" },
  { id: "spec", label: "Spec" },
];

function Inner() {
  const [tab, setTab] = useState<Tab>("assets");

  return (
    <div className="app">
      <nav className="nav">
        <div className="brand">
          <span className="brand-dot" />
          BrandForge
        </div>
        <div className="nav-tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`tab ${tab === t.id ? "tab-active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
      </nav>

      <main className="main">
        {tab === "assets" && <AssetsPage />}
        {tab === "apply" && <ApplyPage />}
        {tab === "plan" && <PlanPage />}
        {tab === "spec" && <SpecPage />}
      </main>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <Inner />
    </QueryClientProvider>
  );
}
