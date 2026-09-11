' Launches LedgerBooks with no visible console window, waits for it to come up,
' then opens it in your default browser. Double-click this file (or make a
' desktop shortcut to it) instead of using a terminal.
'
' To stop the server: open Task Manager, find "pythonw.exe", End Task.
' (There's no window to close since this runs silently in the background.)

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = scriptDir
shell.Run """" & scriptDir & "\venv\Scripts\pythonw.exe"" """ & scriptDir & "\serve.py""", 0, False

WScript.Sleep 2000
shell.Run "http://localhost:5057"
