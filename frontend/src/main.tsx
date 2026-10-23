import React from "react";
import ReactDOM from "react-dom/client";
import AuthGate from "./AuthGate";
import RetrievalEvaluationPage from "./RetrievalEvaluationPage";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {new URLSearchParams(window.location.search).get("view") === "retrieval-evaluation"
      ? <RetrievalEvaluationPage /> : <AuthGate />}
  </React.StrictMode>,
);
