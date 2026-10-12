Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = root & "\.venv\Scripts\pythonw.exe"
app = root & "\main.py"
If fso.FileExists(pythonw) Then
  shell.Run """" & pythonw & """ """ & app & """", 0, False
Else
  MsgBox "Please run Install.bat first.", vbExclamation, "Quick Control Center"
End If
