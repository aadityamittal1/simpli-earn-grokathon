"use client";

import { Chart } from "react-chartjs-2";
import {
  Chart as ChartJS,
  BarController,
  BarElement,
  CategoryScale,
  Legend,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  Tooltip,
} from "chart.js";
import annotationPlugin from "chartjs-plugin-annotation";

ChartJS.register(BarController, LineController, BarElement, LineElement, PointElement, CategoryScale, LinearScale, Tooltip, Legend, annotationPlugin);

export type MomentumPoint = { minutes_from_call: number; posts: number; sentiment: number | null; spike?: boolean };

function label(m: number, unit: number) {
  if (unit >= 60) {
    const h = Math.round(m / 60);
    return Math.abs(h) >= 24 ? `${h > 0 ? "+" : ""}${(h / 24).toFixed(1)}d` : `${h > 0 ? "+" : ""}${h}h`;
  }
  return `${m > 0 ? "+" : ""}${m}m`;
}

/** X post volume (bars) + Grok-scored sentiment (line) per time bucket, with a call-start marker. */
export default function MomentumChart({ points, unit, height = 280 }: { points: MomentumPoint[]; unit: number; height?: number }) {
  const labels = points.map((p) => label(p.minutes_from_call, unit));
  const callIdx = points.findIndex((p) => p.minutes_from_call >= 0);
  return (
    <div style={{ height }}>
      <Chart
        type="bar"
        data={{
          labels,
          datasets: [
            {
              type: "bar" as const,
              label: "X posts",
              data: points.map((p) => p.posts),
              backgroundColor: points.map((p) => (p.spike ? "rgba(240,180,80,0.85)" : "rgba(111,217,138,0.35)")),
              borderRadius: 3,
              yAxisID: "y",
              order: 2,
            },
            {
              type: "line" as const,
              label: "Grok sentiment",
              data: points.map((p) => p.sentiment),
              borderColor: "#7fb3ff",
              backgroundColor: "#7fb3ff",
              pointRadius: 0,
              borderWidth: 2,
              tension: 0.35,
              spanGaps: true,
              yAxisID: "s",
              order: 1,
            },
          ],
        }}
        options={{
          responsive: true,
          maintainAspectRatio: false,
          animation: { duration: 250 },
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { labels: { color: "#cfd3d6", boxWidth: 12 } },
            tooltip: { backgroundColor: "#141716" },
            annotation:
              callIdx >= 0
                ? {
                    annotations: {
                      call: {
                        type: "line",
                        xMin: callIdx,
                        xMax: callIdx,
                        borderColor: "rgba(255,255,255,0.55)",
                        borderDash: [4, 4],
                        label: { display: true, content: "Call starts", position: "start", backgroundColor: "#2b2d2c", color: "#f5f5f5", font: { size: 11 } },
                      },
                    },
                  }
                : {},
          },
          scales: {
            x: { ticks: { color: "#9aa0a6", maxTicksLimit: 14, autoSkip: true }, grid: { color: "rgba(255,255,255,0.04)" } },
            y: { position: "left", title: { display: true, text: "posts", color: "#9aa0a6" }, ticks: { color: "#9aa0a6" }, grid: { color: "rgba(255,255,255,0.06)" } },
            s: { position: "right", min: -1, max: 1, title: { display: true, text: "sentiment", color: "#9aa0a6" }, ticks: { color: "#9aa0a6" }, grid: { display: false } },
          },
        }}
      />
    </div>
  );
}
