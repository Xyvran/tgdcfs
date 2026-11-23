import { Typography, Divider, Box } from '@mui/material';
import { ReactNode } from 'react';

interface FormSectionProps {
  title: string;
  children: ReactNode;
  showDivider?: boolean;
}

export function FormSection({ title, children, showDivider = true }: FormSectionProps) {
  return (
    <>
      {showDivider && <Divider sx={{ my: 3 }} />}
      <Typography
        variant="h5"
        gutterBottom
        sx={{ display: 'flex', alignItems: 'center', gap: 1.5, fontWeight: 600 }}
      >
        <Box
          component="span"
          sx={{
            width: 4,
            height: 22,
            borderRadius: 2,
            flexShrink: 0,
            background:
              'linear-gradient(180deg, rgb(var(--dcfs-accent-rgb)), rgba(var(--dcfs-accent-rgb), 0.4))',
          }}
        />
        {title}
      </Typography>
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, mb: 3 }}>
        {children}
      </Box>
    </>
  );
}
