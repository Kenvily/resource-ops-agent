import React from "react";
import { createRoot } from "react-dom/client";
import { OverviewPage } from "./pages/Overview";
import "./style.css";
import "./console.css";
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <OverviewPage />
  </React.StrictMode>,
);
