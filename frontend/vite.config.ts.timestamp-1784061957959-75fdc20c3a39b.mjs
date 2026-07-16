// vite.config.ts
import { defineConfig, loadEnv } from "file:///sessions/happy-amazing-davinci/mnt/Dev%20Projects/live-trading-control-tower_emergent-main/frontend/node_modules/vite/dist/node/index.js";
import react from "file:///sessions/happy-amazing-davinci/mnt/Dev%20Projects/live-trading-control-tower_emergent-main/frontend/node_modules/@vitejs/plugin-react/dist/index.js";
import path from "node:path";
var __vite_injected_original_dirname = "/sessions/happy-amazing-davinci/mnt/Dev Projects/live-trading-control-tower_emergent-main/frontend";
var vite_config_default = defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backendUrl = env.REACT_APP_BACKEND_URL || "";
  const localBackend = env.LOCAL_BACKEND_URL || "http://localhost:8000";
  return {
    plugins: [react()],
    resolve: {
      alias: {
        "@": path.resolve(__vite_injected_original_dirname, "./src")
      }
    },
    server: {
      host: "0.0.0.0",
      port: 3e3,
      strictPort: true,
      proxy: backendUrl ? void 0 : {
        "/api": {
          target: localBackend,
          changeOrigin: true
        }
      }
    },
    preview: {
      host: "0.0.0.0",
      port: 3e3
    },
    define: {
      "import.meta.env.REACT_APP_BACKEND_URL": JSON.stringify(backendUrl),
      "process.env.REACT_APP_BACKEND_URL": JSON.stringify(backendUrl)
    }
  };
});
export {
  vite_config_default as default
};
//# sourceMappingURL=data:application/json;base64,ewogICJ2ZXJzaW9uIjogMywKICAic291cmNlcyI6IFsidml0ZS5jb25maWcudHMiXSwKICAic291cmNlc0NvbnRlbnQiOiBbImNvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9kaXJuYW1lID0gXCIvc2Vzc2lvbnMvaGFwcHktYW1hemluZy1kYXZpbmNpL21udC9EZXYgUHJvamVjdHMvbGl2ZS10cmFkaW5nLWNvbnRyb2wtdG93ZXJfZW1lcmdlbnQtbWFpbi9mcm9udGVuZFwiO2NvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9maWxlbmFtZSA9IFwiL3Nlc3Npb25zL2hhcHB5LWFtYXppbmctZGF2aW5jaS9tbnQvRGV2IFByb2plY3RzL2xpdmUtdHJhZGluZy1jb250cm9sLXRvd2VyX2VtZXJnZW50LW1haW4vZnJvbnRlbmQvdml0ZS5jb25maWcudHNcIjtjb25zdCBfX3ZpdGVfaW5qZWN0ZWRfb3JpZ2luYWxfaW1wb3J0X21ldGFfdXJsID0gXCJmaWxlOi8vL3Nlc3Npb25zL2hhcHB5LWFtYXppbmctZGF2aW5jaS9tbnQvRGV2JTIwUHJvamVjdHMvbGl2ZS10cmFkaW5nLWNvbnRyb2wtdG93ZXJfZW1lcmdlbnQtbWFpbi9mcm9udGVuZC92aXRlLmNvbmZpZy50c1wiO2ltcG9ydCB7IGRlZmluZUNvbmZpZywgbG9hZEVudiB9IGZyb20gJ3ZpdGUnO1xuaW1wb3J0IHJlYWN0IGZyb20gJ0B2aXRlanMvcGx1Z2luLXJlYWN0JztcbmltcG9ydCBwYXRoIGZyb20gJ25vZGU6cGF0aCc7XG5cbmV4cG9ydCBkZWZhdWx0IGRlZmluZUNvbmZpZygoeyBtb2RlIH0pID0+IHtcbiAgY29uc3QgZW52ID0gbG9hZEVudihtb2RlLCBwcm9jZXNzLmN3ZCgpLCAnJyk7XG4gIGNvbnN0IGJhY2tlbmRVcmwgPSBlbnYuUkVBQ1RfQVBQX0JBQ0tFTkRfVVJMIHx8ICcnO1xuXG4gIC8vIExvY2FsIGRldjogd2hlbiBSRUFDVF9BUFBfQkFDS0VORF9VUkwgaXMgdW5zZXQgdGhlIGFwcCBmZXRjaGVzIHJlbGF0aXZlXG4gIC8vIGAvYXBpLypgIHBhdGhzLiBQcm94eSB0aG9zZSB0byB0aGUgbG9jYWwgRmFzdEFQSSBiYWNrZW5kIHNvIHRoZSBmcm9udGVuZFxuICAvLyBhbmQgYmFja2VuZCBzaGFyZSBhbiBvcmlnaW4gKG5vIENPUlMsIG5vIGZyb250ZW5kIGNvbnRyYWN0IGNoYW5nZSkuXG4gIGNvbnN0IGxvY2FsQmFja2VuZCA9IGVudi5MT0NBTF9CQUNLRU5EX1VSTCB8fCAnaHR0cDovL2xvY2FsaG9zdDo4MDAwJztcblxuICByZXR1cm4ge1xuICAgIHBsdWdpbnM6IFtyZWFjdCgpXSxcbiAgICByZXNvbHZlOiB7XG4gICAgICBhbGlhczoge1xuICAgICAgICAnQCc6IHBhdGgucmVzb2x2ZShfX2Rpcm5hbWUsICcuL3NyYycpLFxuICAgICAgfSxcbiAgICB9LFxuICAgIHNlcnZlcjoge1xuICAgICAgaG9zdDogJzAuMC4wLjAnLFxuICAgICAgcG9ydDogMzAwMCxcbiAgICAgIHN0cmljdFBvcnQ6IHRydWUsXG4gICAgICBwcm94eTogYmFja2VuZFVybFxuICAgICAgICA/IHVuZGVmaW5lZFxuICAgICAgICA6IHtcbiAgICAgICAgICAgICcvYXBpJzoge1xuICAgICAgICAgICAgICB0YXJnZXQ6IGxvY2FsQmFja2VuZCxcbiAgICAgICAgICAgICAgY2hhbmdlT3JpZ2luOiB0cnVlLFxuICAgICAgICAgICAgfSxcbiAgICAgICAgICB9LFxuICAgIH0sXG4gICAgcHJldmlldzoge1xuICAgICAgaG9zdDogJzAuMC4wLjAnLFxuICAgICAgcG9ydDogMzAwMCxcbiAgICB9LFxuICAgIGRlZmluZToge1xuICAgICAgJ2ltcG9ydC5tZXRhLmVudi5SRUFDVF9BUFBfQkFDS0VORF9VUkwnOiBKU09OLnN0cmluZ2lmeShiYWNrZW5kVXJsKSxcbiAgICAgICdwcm9jZXNzLmVudi5SRUFDVF9BUFBfQkFDS0VORF9VUkwnOiBKU09OLnN0cmluZ2lmeShiYWNrZW5kVXJsKSxcbiAgICB9LFxuICB9O1xufSk7XG4iXSwKICAibWFwcGluZ3MiOiAiO0FBQTBkLFNBQVMsY0FBYyxlQUFlO0FBQ2hnQixPQUFPLFdBQVc7QUFDbEIsT0FBTyxVQUFVO0FBRmpCLElBQU0sbUNBQW1DO0FBSXpDLElBQU8sc0JBQVEsYUFBYSxDQUFDLEVBQUUsS0FBSyxNQUFNO0FBQ3hDLFFBQU0sTUFBTSxRQUFRLE1BQU0sUUFBUSxJQUFJLEdBQUcsRUFBRTtBQUMzQyxRQUFNLGFBQWEsSUFBSSx5QkFBeUI7QUFLaEQsUUFBTSxlQUFlLElBQUkscUJBQXFCO0FBRTlDLFNBQU87QUFBQSxJQUNMLFNBQVMsQ0FBQyxNQUFNLENBQUM7QUFBQSxJQUNqQixTQUFTO0FBQUEsTUFDUCxPQUFPO0FBQUEsUUFDTCxLQUFLLEtBQUssUUFBUSxrQ0FBVyxPQUFPO0FBQUEsTUFDdEM7QUFBQSxJQUNGO0FBQUEsSUFDQSxRQUFRO0FBQUEsTUFDTixNQUFNO0FBQUEsTUFDTixNQUFNO0FBQUEsTUFDTixZQUFZO0FBQUEsTUFDWixPQUFPLGFBQ0gsU0FDQTtBQUFBLFFBQ0UsUUFBUTtBQUFBLFVBQ04sUUFBUTtBQUFBLFVBQ1IsY0FBYztBQUFBLFFBQ2hCO0FBQUEsTUFDRjtBQUFBLElBQ047QUFBQSxJQUNBLFNBQVM7QUFBQSxNQUNQLE1BQU07QUFBQSxNQUNOLE1BQU07QUFBQSxJQUNSO0FBQUEsSUFDQSxRQUFRO0FBQUEsTUFDTix5Q0FBeUMsS0FBSyxVQUFVLFVBQVU7QUFBQSxNQUNsRSxxQ0FBcUMsS0FBSyxVQUFVLFVBQVU7QUFBQSxJQUNoRTtBQUFBLEVBQ0Y7QUFDRixDQUFDOyIsCiAgIm5hbWVzIjogW10KfQo=
