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
  FilesystemInfo,
  ReplicationQueue,
} from "./manager-client";

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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [fs, q] = await Promise.all([
        managerClient.getFilesystems(),
        managerClient.getReplicationQueue(),
      ]);
      setFilesystems(fs);
      setQueue(q);
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
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
