import { useEffect, useState } from "react";

export type Route = { name: "projects" } | { name: "project"; id: string };

export function parseHash(hash: string): Route {
  const m = /^#\/p\/([A-Za-z0-9_]+)$/.exec(hash);
  return m ? { name: "project", id: m[1] } : { name: "projects" };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseHash(location.hash));
  useEffect(() => {
    const on = () => setRoute(parseHash(location.hash));
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return route;
}
