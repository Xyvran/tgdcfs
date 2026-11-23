"use client";

import { Box } from "@mui/material";
import { useCallback, useEffect, useRef, useState } from "react";

interface ColumnResizeHandleProps {
  containerRef: React.RefObject<HTMLElement | null>;
  width: number;
  minWidth?: number;
  reserved?: number;
  onChange: (width: number) => void;
  onCommit: (width: number) => void;
  onReset: () => void;
}

export function ColumnResizeHandle({
  containerRef,
  width,
  minWidth = 320,
  reserved = 480,
  onChange,
  onCommit,
  onReset,
}: ColumnResizeHandleProps) {
  const [dragging, setDragging] = useState(false);
  const latest = useRef(width);
  const widthRef = useRef(width);

  useEffect(() => {
    widthRef.current = width;
  }, [width]);

  const clamp = useCallback(
    (next: number): number => {
      const container = containerRef.current;
      const max = container
        ? container.clientWidth - reserved
        : minWidth + 320;
      return Math.min(Math.max(next, minWidth), Math.max(minWidth, max));
    },
    [containerRef, minWidth, reserved]
  );

  const handlePointerMove = useCallback(
    (event: PointerEvent) => {
      const container = containerRef.current;
      if (!container) return;
      const rect = container.getBoundingClientRect();
      const next = clamp(rect.right - event.clientX);
      latest.current = next;
      onChange(next);
    },
    [clamp, containerRef, onChange]
  );

  const stopDragging = useCallback(() => {
    setDragging(false);
    window.removeEventListener("pointermove", handlePointerMove);
    window.removeEventListener("pointerup", stopDragging);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    onCommit(latest.current);
  }, [handlePointerMove, onCommit]);

  const startDragging = useCallback(
    (event: React.PointerEvent) => {
      event.preventDefault();
      latest.current = widthRef.current;
      setDragging(true);
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      window.addEventListener("pointermove", handlePointerMove);
      window.addEventListener("pointerup", stopDragging);
    },
    [handlePointerMove, stopDragging]
  );

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      const step = 32;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        const next = clamp(widthRef.current + step);
        widthRef.current = next;
        onChange(next);
        onCommit(next);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        const next = clamp(widthRef.current - step);
        widthRef.current = next;
        onChange(next);
        onCommit(next);
      }
    },
    [clamp, onChange, onCommit]
  );

  return (
    <Box
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize sidebar"
      aria-valuenow={Math.round(width)}
      tabIndex={0}
      onPointerDown={startDragging}
      onDoubleClick={onReset}
      onKeyDown={handleKeyDown}
      sx={{
        display: { xs: "none", md: "flex" },
        alignItems: "center",
        justifyContent: "center",
        width: 10,
        flexShrink: 0,
        cursor: "col-resize",
        touchAction: "none",
        outline: "none",
        "&::after": {
          content: '""',
          width: 4,
          height: 56,
          borderRadius: 2,
          backgroundColor: dragging
            ? "rgb(var(--dcfs-accent-rgb))"
            : "rgba(148, 163, 184, 0.3)",
          transition: "background-color 0.15s ease",
        },
        "&:hover::after": {
          backgroundColor: "rgba(var(--dcfs-accent-rgb), 0.6)",
        },
        "&:focus-visible::after": {
          backgroundColor: "rgb(var(--dcfs-accent-rgb))",
        },
      }}
    />
  );
}
