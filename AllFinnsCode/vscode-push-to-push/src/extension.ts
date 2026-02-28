import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as path from 'path';

let pythonProcess: cp.ChildProcess | undefined;
let statusBarItem: vscode.StatusBarItem;
let outputChannel: vscode.OutputChannel;

export function activate(context: vscode.ExtensionContext) {
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
        } else {
            startMonitoring(context);
        }
    });

    context.subscriptions.push(toggleCommand, statusBarItem, outputChannel);
}

function startMonitoring(context: vscode.ExtensionContext) {
    const config = vscode.workspace.getConfiguration('pushtopush');
    const pythonPath = config.get<string>('pythonPath') || 'python';
    const sensitivity = config.get<number>('sensitivity') || 0.15;
    const pollMs = config.get<number>('pollMs') || 2000;
    const cameraIndex = config.get<number>('cameraIndex') || 0;

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

function updateStatusBar(isActive: boolean) {
    if (isActive) {
        statusBarItem.text = `$(broadcast) Push-to-Push (Active)`;
        statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
        statusBarItem.tooltip = "Click to stop webcam depth monitoring";
    } else {
        statusBarItem.text = `$(stop-circle) Push-to-Push (Off)`;
        statusBarItem.backgroundColor = undefined;
        statusBarItem.tooltip = "Click to start webcam depth monitoring";
    }
}

export function deactivate() {
    stopMonitoring();
}
