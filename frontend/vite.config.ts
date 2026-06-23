import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 프로덕션에서는 FastAPI가 정적 파일과 /api 를 같은 오리진으로 서빙한다.
// 로컬 개발에서는 /api 요청을 백엔드(uvicorn 8000)로 프록시한다.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
