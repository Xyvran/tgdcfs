"use client";

import { Palette, RestartAlt } from "@mui/icons-material";
import {
  Box,
  Button,
  Fab,
  FormControlLabel,
  Popover,
  Switch,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from "@mui/material";
import { useState } from "react";
import {
  ACCENT_PRESETS,
  BackgroundStyle,
  useAppearance,
} from "./AppearanceProvider";

const panelSx = {
  p: 3,
  width: 300,
  mt: 1,
  bgcolor: "#0d1526",
  color: "#e2e8f0",
  border: "1px solid rgba(148, 163, 184, 0.2)",
  borderRadius: 3,
  boxShadow: "0 24px 60px rgba(2, 6, 23, 0.6)",
} as const;

const labelSx = {
  display: "block",
  fontSize: "0.75rem",
  fontWeight: 600,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  color: "#94a3b8",
  mb: 1.5,
} as const;

const toggleSx = {
  color: "#94a3b8",
  borderColor: "rgba(148, 163, 184, 0.25)",
  textTransform: "none",
  "&.Mui-selected": {
    color: "#fff",
    backgroundColor: "rgba(var(--dcfs-accent-rgb), 0.35)",
    "&:hover": {
      backgroundColor: "rgba(var(--dcfs-accent-rgb), 0.45)",
    },
  },
} as const;

export function CustomizePanel() {
  const {
    settings,
    mounted,
    setAccent,
    setBackground,
    setEffects,
    setFullWidth,
    resetAppearance,
  } = useAppearance();
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);

  if (!mounted) return null;

  return (
    <>
      <Tooltip title="Customize appearance">
        <Fab
          aria-label="Customize appearance"
          onClick={(e) => setAnchorEl(e.currentTarget)}
          sx={{
            position: "fixed",
            bottom: 24,
            right: 24,
            zIndex: 1200,
            background:
              "linear-gradient(135deg, rgb(var(--dcfs-accent-rgb)), rgba(var(--dcfs-accent-rgb), 0.7))",
            color: "#fff",
            boxShadow:
              "0 10px 30px rgba(var(--dcfs-accent-rgb), 0.45), 0 4px 12px rgba(2, 6, 23, 0.5)",
            "&:hover": {
              background:
                "linear-gradient(135deg, rgb(var(--dcfs-accent-rgb)), rgba(var(--dcfs-accent-rgb), 0.85))",
              filter: "brightness(1.08)",
            },
          }}
        >
          <Palette />
        </Fab>
      </Tooltip>

      <Popover
        open={Boolean(anchorEl)}
        anchorEl={anchorEl}
        onClose={() => setAnchorEl(null)}
        anchorOrigin={{ vertical: "top", horizontal: "right" }}
        transformOrigin={{ vertical: "bottom", horizontal: "right" }}
        slotProps={{ paper: { sx: panelSx, elevation: 0 } }}
      >
        <Typography
          variant="subtitle1"
          sx={{ fontWeight: 700, color: "#f1f5f9", mb: 2 }}
        >
          Appearance
        </Typography>

        <Typography sx={labelSx}>Accent color</Typography>
        <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1, mb: 2 }}>
          {ACCENT_PRESETS.map((preset) => {
            const selected =
              settings.accent.toLowerCase() === preset.value.toLowerCase();
            return (
              <Tooltip key={preset.value} title={preset.name}>
                <Box
                  component="button"
                  aria-label={`Accent color ${preset.name}`}
                  onClick={() => setAccent(preset.value)}
                  sx={{
                    width: 32,
                    height: 32,
                    borderRadius: "50%",
                    border: "2px solid",
                    borderColor: selected ? "#f8fafc" : "transparent",
                    padding: "2px",
                    cursor: "pointer",
                    background: "transparent",
                  }}
                >
                  <Box
                    sx={{
                      width: "100%",
                      height: "100%",
                      borderRadius: "50%",
                      backgroundColor: preset.value,
                    }}
                  />
                </Box>
              </Tooltip>
            );
          })}
        </Box>

        <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, mb: 2.5 }}>
          <Typography sx={{ fontSize: "0.85rem", color: "#cbd5e1" }}>
            Custom
          </Typography>
          <input
            type="color"
            aria-label="Custom accent color"
            value={settings.accent}
            onChange={(e) => setAccent(e.target.value)}
            style={{
              width: 42,
              height: 28,
              padding: 0,
              border: "1px solid rgba(148, 163, 184, 0.3)",
              borderRadius: 6,
              background: "transparent",
              cursor: "pointer",
            }}
          />
        </Box>

        <Typography sx={labelSx}>Background</Typography>
        <ToggleButtonGroup
          exclusive
          size="small"
          value={settings.background}
          onChange={(_, value: BackgroundStyle | null) => {
            if (value !== null) setBackground(value);
          }}
          fullWidth
          sx={{ mb: 2.5 }}
        >
          <ToggleButton value="aurora" sx={toggleSx}>
            Aurora
          </ToggleButton>
          <ToggleButton value="midnight" sx={toggleSx}>
            Midnight
          </ToggleButton>
          <ToggleButton value="solid" sx={toggleSx}>
            Solid
          </ToggleButton>
        </ToggleButtonGroup>

        <Typography sx={labelSx}>Layout width</Typography>
        <ToggleButtonGroup
          exclusive
          size="small"
          value={settings.fullWidth ? "full" : "normal"}
          onChange={(_, value: "full" | "normal" | null) => {
            if (value !== null) setFullWidth(value === "full");
          }}
          fullWidth
          sx={{ mb: 2.5 }}
        >
          <ToggleButton value="normal" sx={toggleSx}>
            Normal
          </ToggleButton>
          <ToggleButton value="full" sx={toggleSx}>
            Full width
          </ToggleButton>
        </ToggleButtonGroup>

        <Typography sx={labelSx}>Effects</Typography>
        <FormControlLabel
          control={
            <Switch
              checked={settings.effects}
              onChange={(e) => setEffects(e.target.checked)}
              sx={{
                "& .MuiSwitch-switchBase.Mui-checked": {
                  color: "rgb(var(--dcfs-accent-rgb))",
                },
                "& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track": {
                  backgroundColor: "rgba(var(--dcfs-accent-rgb), 0.6)",
                },
              }}
            />
          }
          label={
            <Typography sx={{ fontSize: "0.85rem", color: "#cbd5e1" }}>
              Glow &amp; animations
            </Typography>
          }
        />

        <Button
          fullWidth
          size="small"
          variant="outlined"
          startIcon={<RestartAlt />}
          onClick={resetAppearance}
          sx={{
            mt: 2,
            textTransform: "none",
            color: "#94a3b8",
            borderColor: "rgba(148, 163, 184, 0.3)",
            "&:hover": {
              borderColor: "rgba(148, 163, 184, 0.5)",
              backgroundColor: "rgba(148, 163, 184, 0.08)",
            },
          }}
        >
          Reset to default
        </Button>
      </Popover>
    </>
  );
}
