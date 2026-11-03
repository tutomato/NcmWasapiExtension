#define UNICODE
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>
static WCHAR cached[32768];
__declspec(dllexport) int cef_execute_process(const void *args,void *app,void *sandbox) {
    LPCWSTR wide=GetCommandLineW(); LPCSTR narrow=GetCommandLineA();
    if (!wide || !narrow) return -2;
    wcsncpy(cached,wide,32767); cached[32767]=0;
    return -1;
}
__declspec(dllexport) int cef_initialize(const void *args,const void *settings,void *app,void *sandbox) {
    WCHAR target[32768];
    if (!GetEnvironmentVariableW(L"NCM_AUTOLOAD_TEST_RESULT",target,32768)) return 0;
    HANDLE file=CreateFileW(target,GENERIC_WRITE,FILE_SHARE_READ,NULL,CREATE_ALWAYS,0,NULL);
    if (file==INVALID_HANDLE_VALUE) return 0;
    DWORD written; WriteFile(file,cached,(DWORD)(wcslen(cached)*2),&written,NULL); CloseHandle(file);
    return 1;
}
