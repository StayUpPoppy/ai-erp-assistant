"use strict";

const { spawn } = require("child_process");
const path = require("path");

const apiDir = path.join(__dirname, "..");
const venvPy =
  process.platform === "win32"
    ? path.join(apiDir, ".venv", "Scripts", "python.exe")
    : path.join(apiDir, ".venv", "bin", "python");

const args = ["-m", "pytest", ...process.argv.slice(2)];
const child = spawn(venvPy, args, { cwd: apiDir, stdio: "inherit", windowsHide: true });
child.on("exit", (code) => process.exit(code === null ? 1 : code));
