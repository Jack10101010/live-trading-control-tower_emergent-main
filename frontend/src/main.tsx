import React, { Suspense } from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import { Toaster } from 'sonner';
import App from './App';
import { AppErrorBoundary } from './components/AppErrorBoundary';
import { queryClient } from './lib/queryClient';
import './styles/tokens.css';

function BootFallback() {
  return (
    <div
      className="fixed inset-0 flex items-center justify-center"
      style={{ background: 'var(--bg)' }}
    >
      <div className="flex flex-col items-center gap-4">
        <div
          className="w-8 h-8 rounded-md flex items-center justify-center relative"
          style={{ background: 'linear-gradient(140deg, #4C82F7 0%, #2FB6C9 100%)' }}
        >
          <span className="text-[11px] font-bold text-white mono">CT</span>
          <span className="absolute right-0.5 bottom-0.5 text-[8px] text-white/70">◆</span>
        </div>
        <div className="flex flex-col items-center gap-2">
          <div className="text-xs uppercase tracking-widest text-text-2">Control Tower</div>
          <div className="flex items-center gap-1.5 text-2xs text-text-muted mono">
            <span className="w-1 h-1 rounded-full bg-[color:var(--primary)] ct-pulse-dot" />
            <span>Loading world…</span>
          </div>
        </div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        {/* UI-0: outermost boundary. The shell's suspense queries live inside it, so a
            backend outage renders an explicit disconnected state — never a blank page
            and never a silent fixture fallback. Retry clears the cached rejections. */}
        <AppErrorBoundary onRetry={() => queryClient.resetQueries()}>
          <Suspense fallback={<BootFallback />}>
            <App />
          </Suspense>
        </AppErrorBoundary>
        <Toaster
          theme="dark"
          position="bottom-right"
          toastOptions={{
            style: {
              background: 'var(--panel-2)',
              border: '1px solid var(--border)',
              color: 'var(--text)',
              fontFamily: 'IBM Plex Sans, Inter, sans-serif',
              fontSize: 13,
            },
          }}
        />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
