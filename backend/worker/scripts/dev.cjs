"use strict";

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const workerDir = path.join(__dirname, "..");
const venvPy =
  process.platform === "win32"
    ? path.join(workerDir, "..", "api", ".venv", "Scripts", "python.exe")
    : path.join(workerDir, "..", "api", ".venv", "bin", "python");

function pickPython() {
  const candidates = [venvPy, process.platform === "win32" ? "python" : "python3", "python"];
  for (const candidate of candidates) {
    if (candidate === venvPy && !fs.existsSync(candidate)) continue;
    const probe = spawnSync(candidate, ["-c", "import sys; print(sys.executable)"], {
      cwd: workerDir,
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
    "[worker] No usable Python interpreter found.\n" +
      "Create backend/api/.venv or install Python and backend/worker requirements.\n",
  );
  process.exit(1);
}

const pythonExe = pickedPython.python;
if (pythonExe !== venvPy) {
  console.warn(`[worker] Falling back to system Python: ${pickedPython.executable}`);
}

const child = spawn(pythonExe, ["worker.py"], {
  cwd: workerDir,
  stdio: "inherit",
  windowsHide: true,
});

child.on("exit", (code) => process.exit(code === null ? 1 : code));
