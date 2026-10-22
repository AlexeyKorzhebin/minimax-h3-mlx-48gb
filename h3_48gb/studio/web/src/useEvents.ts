import { useEffect, useRef } from "react";
import { API } from "./api/client";

/** O21: subscribe after `afterSeq` (И10: the snapshot's last_seq) and call `onEvent` for the
 *  listed kinds of THIS project only — the journal is shared by all projects. EventSource itself
 *  resends Last-Event-ID on reconnect. */
export function useEvents(projectId: string, afterSeq: number | null, kinds: string[],
                          onEvent: (kind: string, seq: number) => void) {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  const kindsKey = kinds.join(",");
  useEffect(() => {
    if (afterSeq === null) return;
    const source = new EventSource(`${API}/events?after=${afterSeq}`);
    for (const kind of kindsKey.split(",")) {
      source.addEventListener(kind, (e) => {
        const message = e as MessageEvent;
        if (JSON.parse(message.data).project_id !== projectId) return;
        handler.current(kind, Number(message.lastEventId));
      });
    }
    return () => source.close();
  }, [projectId, afterSeq, kindsKey]);
}
