// Wave 1.5 DOM wiring scenarios; usage: node _ui_gaps_check.mjs <appUrl> <scenario>
import { routes, calls, alerts, prompts, answers, getElementById, start, ok, PROJECT, sleep } from "./_ui_harness.mjs";

const [, , appUrl, scenario] = process.argv;
const fail = (m) => { process.stderr.write(`${m}\n`); process.exit(1); };
const posts = () => calls.filter((c) => c.method === "POST").map((c) => [c.url, c.body]);

const SCENARIOS = {
  async new_video() {
    answers.prompt = "Бой";
    await start(appUrl, { "POST /api/projects": ok({ ok: true, id: "p9", project: {} }),
      "GET /api/projects/p9": ok(PROJECT({ id: "p9", title: "Бой", stages: { script: "draft",
        scenes: "draft", upscale: "draft", assembly: "draft" } })) });
    getElementById("project-new-video").__listeners.click[0]();
    await sleep(80);
    return { prompts, posts: posts(), opened: getElementById("project-modal").hidden === false,
             title: getElementById("project-title").textContent };
  },
  async new_video_cancel() {
    answers.prompt = null;
    await start(appUrl, {});
    getElementById("project-new-video").__listeners.click[0]();
    await sleep(80);
    // cancelling must be silent: no POST, no alert, and showError (which fills #err) never ran
    return { posts: posts(), alerts, errHtml: getElementById("err").innerHTML };
  },
  async new_chat() {
    await start(appUrl, { "POST /api/chat": ok({ ok: true, id: "c1" }) });
    getElementById("project-new-chat").__listeners.click[0]();
    await sleep(80);
    return { posts: posts() };
  },
};

const run = SCENARIOS[scenario];
if (!run) fail(`unknown scenario ${scenario}`);
run().then((out) => { process.stdout.write(JSON.stringify(out)); process.exit(0); },
           (e) => fail(`${e && e.stack || e}`));
