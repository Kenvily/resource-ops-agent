import type { Overview, Status, ClusterId } from "./resource";
export type WindowSize =
  "1h" | "1d" | "1w" | "1mo" | "3mo" | "all" | "24h" | "7d" | "4mo";
export interface TrendPoint {
  value?: number | null;
  timestamp: number;
  capacity_raw: number | null;
  allocated_raw: number | null;
  unallocated_raw: number | null;
  allocation_ratio: number | null;
}
export interface TrendSeries {
  source_start_at?: number | null;
  database_read_at?: number | null;
  history_collected_at?: number | null;
  expected_points?: number | null;
  observed_points?: number | null;
  missing_points?: number | null;
  invalid_points?: number | null;
  id: ClusterId;
  label: string;
  status: Status;
  points: TrendPoint[];
  warnings: string[];
}
export interface TrendResponse {
  sampling?: "hourly_point" | "minute_statistics";
  mode: "mock" | "live";
  units: Overview["units"];
  window: WindowSize;
  start: number;
  end: number;
  step: number;
  fetched_at: number;
  complete: boolean;
  cache_status: "miss" | "hit";
  cache_age_seconds: number;
  series: TrendSeries[];
}
