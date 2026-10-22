import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AppHeader } from "@/components/AppHeader";
import { useRoute } from "@/router";
import { ProjectScreen } from "@/screens/ProjectScreen";
import { ProjectsScreen } from "@/screens/ProjectsScreen";
import "./styles.css";

function App() {
  const route = useRoute();
  return (
    <>
      <AppHeader />
      {route.name === "project" ? <ProjectScreen key={route.id} id={route.id} /> : <ProjectsScreen />}
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
