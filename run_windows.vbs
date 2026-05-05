Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
mainPath = fso.BuildPath(scriptDir, "main.py")

Function FirstExistingPath(paths)
    Dim i
    For i = 0 To UBound(paths)
        If fso.FileExists(paths(i)) Then
            FirstExistingPath = paths(i)
            Exit Function
        End If
    Next
    FirstExistingPath = ""
End Function

localAppData = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
pythonCandidates = Array( _
    fso.BuildPath(localAppData, "Programs\Python\Python313\pythonw.exe"), _
    fso.BuildPath(localAppData, "Programs\Python\Python312\pythonw.exe"), _
    fso.BuildPath(localAppData, "Programs\Python\Python311\pythonw.exe"), _
    fso.BuildPath(localAppData, "Programs\Python\Python310\pythonw.exe"), _
    fso.BuildPath(localAppData, "Programs\Python\Python39\pythonw.exe") _
)

pythonExe = FirstExistingPath(pythonCandidates)
If pythonExe = "" Then
    pythonExe = "pythonw"
End If

shell.CurrentDirectory = scriptDir
shell.Run """" & pythonExe & """ """ & mainPath & """", 0, False
