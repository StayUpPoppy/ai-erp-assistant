"use strict";

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");

const apiDir = path.join(__dirname, "..");
const venvPy =
  process.platform === "win32"
    ? path.join(apiDir, ".venv", "Scripts", "python.exe")
    : path.join(apiDir, ".venv", "bin", "python");

function pickPython() {
  const candidates = [venvPy, process.platform === "win32" ? "python" : "python3", "python"];
  for (const candidate of candidates) {
    if (candidate === venvPy && !fs.existsSync(candidate)) continue;
    const probe = spawnSync(candidate, ["-c", "import sys; print(sys.executable)"], {
      cwd: apiDir,
      encoding: "utf8",
      windowsHide: true,
      shell: false,
    });
    if (probe.status === 0) {
      return { python: candidate, executable: (probe.stdout || "").trim() || candidate };
    }
  }
  return null;
}

const pickedPython = pickPython();
if (!pickedPython) {
  console.error(
    "[api] No usable Python interpreter found.\n" +
      "Create backend/api/.venv or install Python and dependencies, for example:\n" +
      "  cd backend/api\n" +
      "  python -m venv .venv\n" +
      "  .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n",
  );
  process.exit(1);
}

const pythonExe = pickedPython.python;
if (pythonExe !== venvPy) {
  console.warn(`[api] Falling back to system Python: ${pickedPython.executable}`);
}

// Keep this aligned with frontend/.env.local and frontend/next.config.mjs.
const port = (process.env.API_PORT || "8020").trim() || "8020";
const host = (process.env.API_HOST || "127.0.0.1").trim() || "127.0.0.1";

const pdfCheck = spawnSync(pythonExe, ["-c", "import pypdf, fitz"], {
  cwd: apiDir,
  encoding: "utf8",
  windowsHide: true,
});
if (pdfCheck.status !== 0) {
  console.error(
    "[api] Current Python is missing PDF parsing dependencies (pypdf / pymupdf).\n" +
      `Install them with:\n  ${pythonExe} -m pip install -r backend/api/requirements.txt\n`,
  );
  if (pdfCheck.stderr) console.error(pdfCheck.stderr);
  process.exit(1);
}

const useReload =
  process.platform === "win32"
    ? process.env.API_RELOAD === "1"
    : process.env.API_RELOAD !== "0";
if (process.platform === "win32" && !useReload) {
  console.log("[api] Windows detected: running without --reload unless API_RELOAD=1 is set.");
}

const uvicornArgs = ["-m", "uvicorn", "main:app", "--host", host, "--port", port];
if (useReload) uvicornArgs.push("--reload");

const envFile = path.join(apiDir, ".env");
if (fs.existsSync(envFile)) {
  uvicornArgs.push("--env-file", envFile);
  console.log("[api] Using --env-file:", envFile);
}

function spawnUvicorn() {
  const child = spawn(pythonExe, uvicornArgs, {
    cwd: apiDir,
    stdio: "inherit",
    windowsHide: true,
  });
  child.on("exit", (code) => process.exit(code === null ? 1 : code));
}

const probe = net.createServer();
probe.once("error", (err) => {
  if (err.code === "EADDRINUSE") {
    const hint =
      process.platform === "win32"
        ? `netstat -ano | findstr :${port}   then taskkill /PID <PID> /F`
        : `lsof -i :${port} or ss -tlnp | grep :${port}`;
    console.error(`[api] ${host}:${port} is already in use.\n${hint}`);
    process.exit(1);
  }
  console.error("[api] Port probe failed:", err.message);
  process.exit(1);
});
probe.listen({ port: Number(port), host, exclusive: true }, () => {
  probe.close(() => spawnUvicorn());
});
