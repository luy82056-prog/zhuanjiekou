Unicode True
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!ifndef ROOT
  !define ROOT ".."
!endif

!define APP_NAME "转接口"
!define APP_VERSION "1.11.0"
!define APP_PUBLISHER "转接口"
!define APP_EXE "ZhuanJieKou.exe"
!define INSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZhuanJieKou"

Var HadPreviousInstall

Name "${APP_NAME} ${APP_VERSION}"
OutFile "${ROOT}\release\转接口-Setup-${APP_VERSION}-x64.exe"
InstallDir "$LOCALAPPDATA\Programs\ZhuanJieKou"
InstallDirRegKey HKCU "${INSTALL_KEY}" "InstallLocation"
RequestExecutionLevel user
BrandingText "转接口 ${APP_VERSION}"
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "1.11.0.0"
VIAddVersionKey /LANG=2052 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=2052 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "FileDescription" "公开链接转逐字稿与离线文件处理工具"
VIAddVersionKey /LANG=2052 "LegalCopyright" "第三方许可见安装目录 THIRD_PARTY_NOTICES.txt"

!define MUI_ICON "${ROOT}\assets\zhuanjiekou.ico"
!define MUI_UNICON "${ROOT}\assets\zhuanjiekou.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "运行转接口"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
  StrCpy $HadPreviousInstall "0"
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "转接口仅支持 Windows 10/11 x64。"
    Abort
  ${EndIf}
FunctionEnd

Function .onInstFailed
  ${If} $HadPreviousInstall == "1"
    RMDir /r "$INSTDIR"
    Rename "$INSTDIR.rollback" "$INSTDIR"
  ${EndIf}
FunctionEnd

Section "安装转接口" SecMain
  SetShellVarContext current

  ; 覆盖升级前暂存旧版；如果安装失败，.onInstFailed 会自动恢复。
  ; 个人设置位于 %LocalAppData%\AudioXhl，不在程序目录。
  RMDir /r "$INSTDIR.rollback"
  IfFileExists "$INSTDIR\${APP_EXE}" 0 no_previous_install
  Rename "$INSTDIR" "$INSTDIR.rollback"
  StrCpy $HadPreviousInstall "1"
  Goto install_new_files
  no_previous_install:
    RMDir /r "$INSTDIR"
  install_new_files:
  SetOutPath "$INSTDIR"
  File /r "${ROOT}\dist\ZhuanJieKou\*.*"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\转接口"
  CreateShortcut "$SMPROGRAMS\转接口\转接口.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\assets\zhuanjiekou.ico"
  CreateShortcut "$SMPROGRAMS\转接口\卸载转接口.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortcut "$DESKTOP\转接口.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\assets\zhuanjiekou.ico"

  WriteRegStr HKCU "${INSTALL_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${INSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${INSTALL_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${INSTALL_KEY}" "DisplayIcon" "$INSTDIR\assets\zhuanjiekou.ico"
  WriteRegStr HKCU "${INSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${INSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${INSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${INSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${INSTALL_KEY}" "NoRepair" 1
  RMDir /r "$INSTDIR.rollback"
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$DESKTOP\转接口.lnk"
  RMDir /r "$SMPROGRAMS\转接口"
  DeleteRegKey HKCU "${INSTALL_KEY}"
  RMDir /r "$INSTDIR"
  ; 刻意保留 %LocalAppData%\AudioXhl 及用户选择的下载目录。
SectionEnd
