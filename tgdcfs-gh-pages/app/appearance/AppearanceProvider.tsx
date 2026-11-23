"use client";

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

export type BackgroundStyle = "aurora" | "midnight" | "solid";

export interface AppearanceSettings {
  accent: string;
  background: BackgroundStyle;
  effects: boolean;
  sidebarWidth: number;
  fullWidth: boolean;
}

export const ACCENT_PRESETS: { name: string; value: string }[] = [
  { name: "Telegram Blue", value: "#2aabee" },
  { name: "Blurple", value: "#5865f2" },
  { name: "Teal", value: "#14b8a6" },
  { name: "Green", value: "#22c55e" },
  { name: "Amber", value: "#f59e0b" },
  { name: "Pink", value: "#ec4899" },
  { name: "Purple", value: "#a855f7" },
  { name: "Red", value: "#ef4444" },
];

const STORAGE_KEY = "tgdcfs-appearance";

export const DEFAULT_SETTINGS: AppearanceSettings = {
  accent: "#2aabee",
  background: "aurora",
  effects: true,
  sidebarWidth: 400,
  fullWidth: false,
};

function hexToRgbTriplet(hex: string): string {
  const normalized = hex.replace("#", "");
  const full =
    normalized.length === 3
      ? normalized
          .split("")
          .map((c) => c + c)
          .join("")
      : normalized;
  const num = parseInt(full, 16);
  if (Number.isNaN(num)) {
    return hexToRgbTriplet(DEFAULT_SETTINGS.accent);
  }
  const r = (num >> 16) & 255;
  const g = (num >> 8) & 255;
  const b = num & 255;
  return `${r}, ${g}, ${b}`;
}

interface AppearanceContextValue {
  settings: AppearanceSettings;
  mounted: boolean;
  setAccent: (accent: string) => void;
  setBackground: (background: BackgroundStyle) => void;
  setEffects: (effects: boolean) => void;
  setSidebarWidth: (width: number) => void;
  setFullWidth: (fullWidth: boolean) => void;
  resetAppearance: () => void;
}

const AppearanceContext = createContext<AppearanceContextValue | null>(null);

export function AppearanceProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [settings, setSettings] =
    useState<AppearanceSettings>(DEFAULT_SETTINGS);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw) as Partial<AppearanceSettings>;
        setSettings({ ...DEFAULT_SETTINGS, ...parsed });
      }
    } catch {
      setSettings(DEFAULT_SETTINGS);
    }
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!mounted) return;
    const root = document.documentElement;
    root.style.setProperty("--dcfs-accent", settings.accent);
    root.style.setProperty(
      "--dcfs-accent-rgb",
      hexToRgbTriplet(settings.accent)
    );
    root.dataset.bg = settings.background;
    root.dataset.effects = settings.effects ? "on" : "off";
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    } catch {
      return;
    }
  }, [settings, mounted]);

  const value = useMemo<AppearanceContextValue>(
    () => ({
      settings,
      mounted,
      setAccent: (accent) => setSettings((prev) => ({ ...prev, accent })),
      setBackground: (background) =>
        setSettings((prev) => ({ ...prev, background })),
      setEffects: (effects) => setSettings((prev) => ({ ...prev, effects })),
      setSidebarWidth: (sidebarWidth) =>
        setSettings((prev) => ({ ...prev, sidebarWidth })),
      setFullWidth: (fullWidth) =>
        setSettings((prev) => ({ ...prev, fullWidth })),
      resetAppearance: () => setSettings(DEFAULT_SETTINGS),
    }),
    [settings, mounted]
  );

  return (
    <AppearanceContext.Provider value={value}>
      {children}
    </AppearanceContext.Provider>
  );
}

export function useAppearance(): AppearanceContextValue {
  const ctx = useContext(AppearanceContext);
  if (!ctx) {
    throw new Error("useAppearance must be used within AppearanceProvider");
  }
  return ctx;
}
