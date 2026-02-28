"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const cp = __importStar(require("child_process"));
const path = __importStar(require("path"));
let pythonProcess;
let statusBarItem;
let outputChannel;
function activate(context) {
    outputChannel = vscode.window.createOutputChannel("Push-to-Push");
    // Create Status Bar Item
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = 'pushtopush.toggle';
    updateStatusBar(false);
    statusBarItem.show();
    // Register Toggle Command
    let toggleCommand = vscode.commands.registerCommand('pushtopush.toggle', () => {
        if (pythonProcess) {
            stopMonitoring();
        }
        else {
            startMonitoring(context);
        }
    });
    context.subscriptions.push(toggleCommand, statusBarItem, outputChannel);
}
function startMonitoring(context) {
    const config = vscode.workspace.getConfiguration('pushtopush');
    const pythonPath = config.get('pythonPath') || 'python';
    const sensitivity = config.get('sensitivity') || 0.15;
    const pollMs = config.get('pollMs') || 2000;
    const cameraIndex = config.get('cameraIndex') || 0;
    // Resolve path to the Python script inside the extension
    const scriptPath = path.join(context.extensionPath, 'python', 'push_detector.py');
    // Resolve repo path (active workspace)
    const repoPath = vscode.workspace.workspaceFolders?.[0].uri.fsPath;
    if (!repoPath) {
        vscode.window.showErrorMessage("Open a workspace folder first to use Push-to-Push.");
        return;
    }
    const args = [
        scriptPath,
        "--repo", repoPath,
        "--sensitivity", sensitivity.toString(),
        "--poll", pollMs.toString(),
        "--camera", cameraIndex.toString()
    ];
    outputChannel.appendLine(`[Extension] Starting: ${pythonPath} ${args.join(' ')}`);
    pythonProcess = cp.spawn(pythonPath, args);
    pythonProcess.stdout?.on('data', (data) => {
        const output = data.toString();
        outputChannel.append(output);
        // Listen for the push event log
        if (output.includes("[EVENT] PUSH DETECTED")) {
            vscode.window.showInformationMessage("Push-to-Push: Physical push detected! Pushing code...");
        }
        if (output.includes("[PUSH] Pushed to origin")) {
            vscode.window.showInformationMessage("Push-to-Push: Code pushed to GitHub!");
        }
        if (output.includes("[ERROR]")) {
            vscode.window.showErrorMessage(`Push-to-Push Internal Error: ${output}`);
        }
    });
    pythonProcess.stderr?.on('data', (data) => {
        outputChannel.appendLine(`[Python Error] ${data.toString()}`);
    });
    pythonProcess.on('close', (code) => {
        outputChannel.appendLine(`[Extension] Python process exited with code ${code}`);
        pythonProcess = undefined;
        updateStatusBar(false);
    });
    updateStatusBar(true);
    vscode.window.showInformationMessage("Push-to-Push depth detection active.");
}
function stopMonitoring() {
    if (pythonProcess) {
        outputChannel.appendLine("[Extension] Stopping Python process...");
        pythonProcess.kill();
        pythonProcess = undefined;
    }
    updateStatusBar(false);
    vscode.window.showInformationMessage("Push-to-Push deactivated.");
}
function updateStatusBar(isActive) {
    if (isActive) {
        statusBarItem.text = `$(broadcast) Push-to-Push (Active)`;
        statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
        statusBarItem.tooltip = "Click to stop webcam depth monitoring";
    }
    else {
        statusBarItem.text = `$(stop-circle) Push-to-Push (Off)`;
        statusBarItem.backgroundColor = undefined;
        statusBarItem.tooltip = "Click to start webcam depth monitoring";
    }
}
function deactivate() {
    stopMonitoring();
}
//# sourceMappingURL=extension.js.map