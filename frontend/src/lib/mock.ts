import type { MatchThread } from "./types";

// Deterministic seed data so the board renders fully offline (no live engine
// required for the build/preview). The dashboard hydrates these and then lets
// the operator reconcile against the real API on demand.

export const MOCK_THREADS: MatchThread[] = [
  {
    match_id: "wc-qf-arg-fra",
    status: "PENDING_APPROVAL",
    interrupted: true,
    interrupt_payload: { checkpoint: "HUMAN_VALIDATION_REQUIRED" },
    next_nodes: ["human_approval"],
    match_stats: {
      competition: "FIFA World Cup",
      stage: "Quarter Final",
      home_team: "Argentina",
      away_team: "France",
      final_score: "3-2",
      possession_pct: { Argentina: 64, France: 36 },
      xg: { Argentina: 1.4, France: 3.1 },
      biggest_anomaly:
        "Argentina owned 64% of the ball and out-shot France 18-7, yet France's xG was more than double.",
    },
    script_raw:
      "France just cooked Argentina on the counter — and the stats make zero sense. Argentina owned the ball, 64% possession, eighteen shots, a passing clinic. But here's the trap: every overload in the left half-space left a runway behind them. France ghosted the press, broke at pace, and turned three counters into daggers. Dominance without protection is just expensive decoration. So tell me below — was this a defensive masterclass, or did Argentina tactically self-destruct?",
    video_prompts: [
      "Holographic football pitch rendered in glowing cyan wireframe, possession heat-bloom pulsing in one half, no human faces",
      "Abstract neon arrows surging through a dark tactical grid, representing a lightning counter-attack, cyber aesthetic",
      "Glowing data nodes collapsing as a high defensive line fractures, particle trails streaking forward, holographic style",
      "Floating translucent stat panels (xG, possession, PPDA) orbiting a luminous ball, sci-fi broadcast overlay",
      "Final scoreline igniting in volumetric light over a shadowed stadium silhouette, electric pulse shockwave, no faces",
    ],
  },
  {
    match_id: "wc-r16-bra-ger",
    status: "PROCESSING_ASSETS",
    interrupted: true,
    interrupt_payload: { checkpoint: "ASSET_UPLOAD_REQUIRED", expected_clips: 5 },
    next_nodes: ["await_assets"],
    uploaded_clips: 2,
    match_stats: {
      competition: "FIFA World Cup",
      stage: "Round of 16",
      home_team: "Brazil",
      away_team: "Germany",
      final_score: "0-4",
      possession_pct: { Brazil: 58, Germany: 42 },
      xg: { Brazil: 0.9, Germany: 3.6 },
      biggest_anomaly: "Germany scored 4 from 6 shots on target in a clinical away dismantling.",
    },
    script_raw:
      "Germany dismantled Brazil 0-4 away from home and barely broke a sweat. Brazil had the ball but Germany had the blueprint — vertical, ruthless, surgical in the final third.",
    video_prompts: [
      "Holographic vertical passing lanes slicing a cyan wireframe pitch, clinical geometry, no faces",
      "Neon goal-trajectory arcs igniting in sequence over a dark tactical grid",
      "Glowing xG bars stacking violently on one side of a sci-fi broadcast panel",
      "Particle shockwave radiating from a luminous ball at the moment of impact",
      "Scoreline 0-4 burning in volumetric orange light over a shadowed arena, no faces",
    ],
  },
  {
    match_id: "wc-grp-esp-eng",
    status: "RENDERING",
    interrupted: false,
    next_nodes: ["process_rendering"],
    match_stats: {
      competition: "FIFA World Cup",
      stage: "Group Stage",
      home_team: "Spain",
      away_team: "England",
      final_score: "1-1",
      possession_pct: { Spain: 61, England: 39 },
      xg: { Spain: 1.8, England: 1.2 },
    },
    script_raw:
      "A 1-1 chess match that looked nothing like the scoreline. Spain probed, England absorbed and struck once on the break.",
    video_prompts: [
      "Holographic possession web pulsing in tight central zones, cyan tactical aesthetic",
      "Two opposing neon pressing structures interlocking on a dark grid",
      "Glowing balance scale of xG tilting marginally, sci-fi data overlay",
      "Single counter-attack arrow streaking through a compact defensive block",
    ],
  },
  {
    match_id: "wc-grp-ned-por",
    status: "COMPLETED",
    interrupted: false,
    next_nodes: [],
    match_stats: {
      competition: "FIFA World Cup",
      stage: "Group Stage",
      home_team: "Netherlands",
      away_team: "Portugal",
      final_score: "5-0",
      possession_pct: { Netherlands: 52, Portugal: 48 },
      xg: { Netherlands: 4.2, Portugal: 0.4 },
    },
    script_raw:
      "Five-nil. Portugal got absolutely ghosted by a Dutch side that turned every transition into a goal.",
    video_prompts: [
      "Cascade of five holographic goal-flares erupting across a cyan wireframe pitch, no faces",
      "Relentless neon transition arrows overwhelming a collapsing defensive grid",
      "Towering xG skyline rendered as glowing data spires, sci-fi overlay",
      "Volumetric 5-0 scoreline detonating in green light over a dark stadium",
    ],
  },
  {
    match_id: "wc-sf-arg-bra",
    status: "SCRAPED",
    interrupted: false,
    next_nodes: ["generate_tactical_script"],
    match_stats: {
      competition: "FIFA World Cup",
      stage: "Semi Final",
      home_team: "Argentina",
      away_team: "Brazil",
      final_score: "2-3",
      possession_pct: { Argentina: 55, Brazil: 45 },
      xg: { Argentina: 2.1, Brazil: 2.4 },
    },
    script_raw: "",
    video_prompts: [],
  },
];
