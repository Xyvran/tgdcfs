"use client";

import { Refresh, Replay } from "@mui/icons-material";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  List,
  ListItem,
  ListItemText,
  Typography,
} from "@mui/material";
import { useCallback, useEffect, useState } from "react";
import ManagerClient, {
  CacheStats,
  FilesystemInfo,
  ReplicationQueue,
} from "./manager-client";

const formatBytes = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[unit]}`;
};

interface ReplicationDialogProps {
  open: boolean;
  onClose: () => void;
  managerClient: ManagerClient;
}

// Mirroring state of every file system: its stores, what is still waiting
// for background replication, and a way to retry failed items or start a
// backfill.
export default function ReplicationDialog({
  open,
  onClose,
  managerClient,
}: ReplicationDialogProps) {
  const [filesystems, setFilesystems] = useState<{
    [name: string]: FilesystemInfo;
  }>({});
  const [queue, setQueue] = useState<ReplicationQueue>({});
  const [cache, setCache] = useState<CacheStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [fs, q, c] = await Promise.all([
        managerClient.getFilesystems(),
        managerClient.getReplicationQueue(),
        managerClient.getCacheStats(),
      ]);
      setFilesystems(fs);
      setQueue(q);
      setCache(c);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [managerClient]);

  useEffect(() => {
    if (open) {
      load();
    }
  }, [open, load]);

  const retry = async (name: string) => {
    try {
      await managerClient.retryReplication(name);
      setNotice(`Retrying queued files of ${name}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Retry failed");
    }
  };

  const backfill = async (name: string) => {
    try {
      const taskId = await managerClient.startBackfill(name);
      setNotice(`Backfill of ${name} started (task ${taskId})`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Backfill failed");
    }
  };

  const evict = async () => {
    try {
      const evicted = await managerClient.evictCache();
      setNotice(`Dropped ${evicted} cache entr${evicted === 1 ? "y" : "ies"}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Eviction failed");
    }
  };

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <Box sx={{ flexGrow: 1 }}>Mirrors and replication</Box>
        <IconButton onClick={load} size="small" disabled={loading}>
          <Refresh />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}
        {notice && (
          <Alert severity="info" sx={{ mb: 2 }} onClose={() => setNotice(null)}>
            {notice}
          </Alert>
        )}
        {loading && Object.keys(filesystems).length === 0 ? (
          <Box sx={{ display: "flex", justifyContent: "center", py: 3 }}>
            <CircularProgress />
          </Box>
        ) : (
          Object.entries(filesystems).map(([name, info]) => {
            const items = queue[name] || [];
            const failed = items.filter((item) => item.attempts > 0);
            return (
              <Box key={name} sx={{ mb: 3 }}>
                <Box
                  sx={{ display: "flex", alignItems: "center", gap: 1, mb: 1 }}
                >
                  <Typography variant="subtitle1" sx={{ flexGrow: 1 }}>
                    {name}
                  </Typography>
                  {info.primary_dead && (
                    <Chip label="primary unreachable" color="error" size="small" />
                  )}
                </Box>
                <Typography variant="body2" color="text.secondary">
                  Primary <b>{info.primary}</b>
                  {info.mirrors.length > 0 ? (
                    <>
                      , mirrors <b>{info.mirrors.join(", ")}</b> ({info.mode},{" "}
                      {info.sync}
                      {info.strict ? ", strict" : ""})
                    </>
                  ) : (
                    ", no mirrors"
                  )}
                  , metadata {info.metadata}
                </Typography>
                {info.mirrors.length > 0 && (
                  <>
                    <Typography variant="body2" sx={{ mt: 1 }}>
                      {items.length === 0
                        ? "Nothing waiting for replication."
                        : `${items.length} file(s) waiting` +
                          (failed.length > 0
                            ? `, ${failed.length} with failed attempts`
                            : "")}
                    </Typography>
                    {items.length > 0 && (
                      <List dense disablePadding>
                        {items.slice(0, 20).map((item) => (
                          <ListItem key={item.path} disableGutters>
                            <ListItemText
                              primary={item.path}
                              secondary={
                                item.attempts > 0
                                  ? `${item.attempts} failed attempt(s): ${
                                      item.last_error || "unknown error"
                                    }`
                                  : "queued"
                              }
                              slotProps={{
                                primary: { sx: { wordBreak: "break-all" } },
                              }}
                            />
                          </ListItem>
                        ))}
                        {items.length > 20 && (
                          <Typography variant="caption" color="text.secondary">
                            and {items.length - 20} more
                          </Typography>
                        )}
                      </List>
                    )}
                    <Box sx={{ display: "flex", gap: 1, mt: 1 }}>
                      <Button
                        size="small"
                        variant="outlined"
                        startIcon={<Replay />}
                        disabled={failed.length === 0}
                        onClick={() => retry(name)}
                      >
                        Retry failed
                      </Button>
                      <Button
                        size="small"
                        variant="outlined"
                        onClick={() => backfill(name)}
                      >
                        Backfill
                      </Button>
                    </Box>
                  </>
                )}
              </Box>
            );
          })
        )}
        {cache && (
          <Box sx={{ mt: 1 }}>
            <Typography variant="subtitle1" sx={{ mb: 1 }}>
              Local cache
            </Typography>
            {!cache.enabled ? (
              <Typography variant="body2" color="text.secondary">
                Off. Enable <code>tgdcfs.cache</code> to stage uploads for the
                mirrors and keep entries for reads.
              </Typography>
            ) : (
              <>
                <Typography variant="body2" color="text.secondary">
                  {cache.entries ?? 0} entr{cache.entries === 1 ? "y" : "ies"} (
                  {cache.complete_entries ?? 0} complete),{" "}
                  {formatBytes(cache.used_bytes ?? 0)}
                  {cache.max_size_bytes
                    ? ` of ${formatBytes(cache.max_size_bytes)}`
                    : ", no size limit"}
                  ; {cache.pinned_entries ?? 0} pinned for the mirrors (
                  {formatBytes(cache.pinned_bytes ?? 0)})
                  {(cache.writing_entries ?? 0) > 0
                    ? `, ${cache.writing_entries} being written`
                    : ""}
                  . Reads: {cache.hits ?? 0} served from disk,{" "}
                  {cache.misses ?? 0} fetched.
                </Typography>
                {cache.per_filesystem &&
                  Object.entries(cache.per_filesystem).map(([name, info]) => (
                    <Typography
                      key={name}
                      variant="body2"
                      color="text.secondary"
                      sx={{ pl: 2 }}
                    >
                      {name}: {info.entries} entr{info.entries === 1 ? "y" : "ies"},{" "}
                      {formatBytes(info.bytes)}, {info.pinned} pinned
                    </Typography>
                  ))}
                <Box sx={{ display: "flex", gap: 1, mt: 1 }}>
                  <Button
                    size="small"
                    variant="outlined"
                    onClick={evict}
                    disabled={(cache.entries ?? 0) - (cache.pinned_entries ?? 0) <= 0}
                  >
                    Drop unpinned entries
                  </Button>
                </Box>
              </>
            )}
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
