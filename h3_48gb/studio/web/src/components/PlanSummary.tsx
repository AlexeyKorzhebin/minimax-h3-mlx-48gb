import type { Plan } from "@/api/types";

export const minutes = (s: number) => (s < 90 ? `≈ ${Math.round(s)} с` : `≈ ${Math.round(s / 60)} мин`);

/** The dry plan before a GPU task (spec §4.4 steps 4–5): time, model switches, what blocks. */
export function PlanSummary({ plan }: { plan: Plan }) {
  return (
    <div className="text-[13px] text-ink2" aria-label="План">
      <span>{`Время ${minutes(plan.total_s)}`}</span>
      {plan.checkpoint_switches.map((s, i) => (
        <span key={i}>{` · смена модели ${s.from ?? "—"} → ${s.to}: +${Math.round(s.seconds)} с`}</span>
      ))}
    </div>
  );
}
