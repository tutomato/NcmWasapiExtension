/* App-local NCM WASAPI autoload. No global registry hooks or injected startup
 * threads. DllMain only patches already resolved IAT slots; all file/socket/
 * helper operations run later, outside the Windows loader lock. */
#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <winsock2.h>
#include <windows.h>
#include <stdint.h>
#include <wchar.h>
#include <stdio.h>
#include <string.h>

#ifndef LOAD_LIBRARY_SEARCH_SYSTEM32
#define LOAD_LIBRARY_SEARCH_SYSTEM32 0x00000800
#endif

typedef int (__cdecl *cef_execute_fn)(const void *, void *, void *);
typedef int (__cdecl *cef_initialize_fn)(const void *, const void *, void *, void *);
static cef_execute_fn real_execute;
static cef_initialize_fn real_initialize;
static LPWSTR (WINAPI *real_command_w)(void);
static LPSTR (WINAPI *real_command_a)(void);
static WCHAR self_directory[32768];
static WCHAR executable_path[32768];
static WCHAR command_w[32768];
static CHAR command_a[65536];
static volatile LONG preparation;
static volatile LONG helper_started;
static volatile LONG command_ready;
static unsigned short debug_port;
static SOCKET reservation = INVALID_SOCKET;
static BOOL own_winsock;
static HANDLE helper_process, ready_event, stop_event;
static INIT_ONCE forwarding_once = INIT_ONCE_STATIC_INIT;
static HMODULE system_msimg;
static HMODULE system_combase;
static FARPROC forwarding[5];

static BOOL full_path(const WCHAR *path) {
    if (!path || !*path) return FALSE;
    return ((path[0] && path[1] == L':' && (path[2] == L'\\' || path[2] == L'/')) ||
            (path[0] == L'\\' && path[1] == L'\\'));
}
static BOOL join_path(WCHAR *output, size_t count, const WCHAR *root, const WCHAR *leaf) {
    size_t n = wcslen(root), m = wcslen(leaf);
    if (!n || n + m + 2 > count) return FALSE;
    wcscpy(output, root);
    if (root[n - 1] != L'\\' && root[n - 1] != L'/') output[n++] = L'\\';
    wcscpy(output + n, leaf);
    return TRUE;
}
static BOOL regular_file(const WCHAR *path) {
    DWORD flags = GetFileAttributesW(path);
    return flags != INVALID_FILE_ATTRIBUTES && !(flags & FILE_ATTRIBUTE_DIRECTORY);
}
static void release_port(void) {
    if (reservation != INVALID_SOCKET) { closesocket(reservation); reservation = INVALID_SOCKET; }
    if (own_winsock) { WSACleanup(); own_winsock = FALSE; }
}
static LPWSTR WINAPI local_command_w(void) {
    return InterlockedCompareExchange(&command_ready, 0, 0) ? command_w : real_command_w();
}
static LPSTR WINAPI local_command_a(void) {
    return InterlockedCompareExchange(&command_ready, 0, 0) ? command_a : real_command_a();
}

/* Called from cef_execute_process, before CEF caches Chromium's command line. */
static BOOL prepare_command(void) {
    LONG previous = InterlockedCompareExchange(&preparation, 1, 0);
    if (previous) return previous == 2;
    const WCHAR *original = GetCommandLineW();
    if (!original || wcslen(original) > 32000 ||
        wcsstr(original, L"--remote-debugging-port") ||
        wcsstr(original, L"--ncm-wasapi-managed-launch") || wcsstr(original, L"--type=")) goto failed;
    WCHAR ini[32768], configured_root[32768], runtime_file[32768], controller_file[32768];
    if (!join_path(ini, 32768, self_directory, L"ncm-wasapi-autoload.ini") ||
        GetPrivateProfileIntW(L"NcmWasapiExtension", L"Schema", 0, ini) != 1 ||
        !GetPrivateProfileStringW(L"NcmWasapiExtension", L"PayloadRoot", L"", configured_root, 32768, ini) ||
        !full_path(configured_root) ||
        !join_path(runtime_file, 32768, configured_root, L"runtime\\python.exe") ||
        !join_path(controller_file, 32768, configured_root, L"controller.py") ||
        !regular_file(runtime_file) || !regular_file(controller_file)) goto failed;
    WSADATA data;
    if (WSAStartup(MAKEWORD(2, 2), &data)) goto failed;
    own_winsock = TRUE;
    reservation = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (reservation == INVALID_SOCKET) goto failed;
    BOOL exclusive = TRUE;
    if (setsockopt(reservation, SOL_SOCKET, SO_EXCLUSIVEADDRUSE,
            (const char *)&exclusive, sizeof(exclusive))) goto failed;
    struct sockaddr_in address;
    ZeroMemory(&address, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(reservation, (struct sockaddr *)&address, sizeof(address))) goto failed;
    int length = sizeof(address);
    if (getsockname(reservation, (struct sockaddr *)&address, &length)) goto failed;
    debug_port = ntohs(address.sin_port);
    if (!debug_port) goto failed;
    int n = swprintf(command_w, 32768, L"%ls --remote-debugging-port=%u --remote-debugging-address=127.0.0.1", original, debug_port);
    if (n <= 0 || n >= 32768 || !WideCharToMultiByte(CP_ACP, 0, command_w, -1,
            command_a, sizeof(command_a), NULL, NULL)) goto failed;
    InterlockedExchange(&command_ready, 1);
    InterlockedExchange(&preparation, 2);
    return TRUE;
failed:
    release_port();
    InterlockedExchange(&preparation, 3);
    return FALSE;
}

static BOOL append_argument(WCHAR *command, size_t count, const WCHAR *value) {
    size_t used = wcslen(command);
    if (used + 4 >= count) return FALSE;
    command[used++] = L' '; command[used++] = L'"';
    size_t slashes = 0;
    for (const WCHAR *p = value; *p; ++p) {
        if (*p == L'\\') { ++slashes; continue; }
        size_t copies = *p == L'"' ? slashes * 2 + 1 : slashes;
        if (used + copies + 3 >= count) return FALSE;
        while (copies--) command[used++] = L'\\';
        command[used++] = *p; slashes = 0;
    }
    if (used + slashes * 2 + 3 >= count) return FALSE;
    while (slashes--) { command[used++] = L'\\'; command[used++] = L'\\'; }
    command[used++] = L'"'; command[used] = 0;
    return TRUE;
}

static BOOL launch_helper(void) {
    WCHAR ini[32768], root[32768], python[32768], controller[32768];
    WCHAR ready_name[192], stop_name[192], number[64], command[32768];
    if (!join_path(ini, 32768, self_directory, L"ncm-wasapi-autoload.ini") ||
        GetPrivateProfileIntW(L"NcmWasapiExtension", L"Schema", 0, ini) != 1 ||
        !GetPrivateProfileStringW(L"NcmWasapiExtension", L"PayloadRoot", L"", root, 32768, ini) ||
        !full_path(root) || !join_path(python, 32768, root, L"runtime\\python.exe") ||
        !join_path(controller, 32768, root, L"controller.py") ||
        !regular_file(python) || !regular_file(controller)) return FALSE;
    FILETIME created, exited, kernel, user;
    if (!GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user)) return FALSE;
    ULARGE_INTEGER stamp; stamp.LowPart = created.dwLowDateTime; stamp.HighPart = created.dwHighDateTime;
    DWORD pid = GetCurrentProcessId();
    swprintf(ready_name, 192, L"Local\\NcmWasapiExtension.Ready.%lu.%llu", (unsigned long)pid, stamp.QuadPart);
    swprintf(stop_name, 192, L"Local\\NcmWasapiExtension.Stop.%lu.%llu", (unsigned long)pid, stamp.QuadPart);
    ready_event = CreateEventW(NULL, TRUE, FALSE, ready_name);
    stop_event = CreateEventW(NULL, TRUE, FALSE, stop_name);
    if (!ready_event || !stop_event) return FALSE;
    command[0] = 0;
    if (!append_argument(command, 32768, python) ||
        !append_argument(command, 32768, L"-B") || !append_argument(command, 32768, L"-u") ||
        !append_argument(command, 32768, controller) || !append_argument(command, 32768, L"--attach")) return FALSE;
    swprintf(number, 64, L"%lu", (unsigned long)pid);
    if (!append_argument(command,32768,number) || !append_argument(command,32768,L"--debug-port")) return FALSE;
    swprintf(number, 64, L"%u", debug_port);
    if (!append_argument(command,32768,number) || !append_argument(command,32768,L"--startup-ready-event") ||
        !append_argument(command,32768,ready_name) || !append_argument(command,32768,L"--stop-event") ||
        !append_argument(command,32768,stop_name) || !append_argument(command,32768,L"--creation-time")) return FALSE;
    swprintf(number, 64, L"%llu", stamp.QuadPart);
    if (!append_argument(command,32768,number) || !append_argument(command,32768,L"--client") ||
        !append_argument(command,32768,executable_path)) return FALSE;

    SECURITY_ATTRIBUTES sa = {sizeof(sa), NULL, TRUE};
    HANDLE null_handle = CreateFileW(L"NUL", GENERIC_READ|GENERIC_WRITE,
        FILE_SHARE_READ|FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, NULL);
    if (null_handle == INVALID_HANDLE_VALUE) return FALSE;
    STARTUPINFOEXW startup; ZeroMemory(&startup, sizeof(startup));
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = null_handle;
    SIZE_T attribute_bytes = 0;
    InitializeProcThreadAttributeList(NULL, 1, 0, &attribute_bytes);
    startup.lpAttributeList = HeapAlloc(GetProcessHeap(), 0, attribute_bytes);
    BOOL attributes = startup.lpAttributeList && InitializeProcThreadAttributeList(startup.lpAttributeList, 1, 0, &attribute_bytes);
    BOOL started = FALSE;
    PROCESS_INFORMATION process; ZeroMemory(&process, sizeof(process));
    if (attributes && UpdateProcThreadAttribute(startup.lpAttributeList, 0,
        PROC_THREAD_ATTRIBUTE_HANDLE_LIST, &null_handle, sizeof(null_handle), NULL, NULL)) {
        started = CreateProcessW(python, command + 1, NULL, NULL, TRUE,
            CREATE_NO_WINDOW|EXTENDED_STARTUPINFO_PRESENT, NULL, root,
            &startup.StartupInfo, &process);
    }
    if (attributes) DeleteProcThreadAttributeList(startup.lpAttributeList);
    if (startup.lpAttributeList) HeapFree(GetProcessHeap(), 0, startup.lpAttributeList);
    CloseHandle(null_handle);
    if (!started) return FALSE;
    CloseHandle(process.hThread); helper_process = process.hProcess;
    HANDLE waiters[2] = {ready_event, helper_process};
    DWORD outcome = WaitForMultipleObjects(2, waiters, FALSE, 15000);
    if (outcome == WAIT_OBJECT_0) return TRUE;
    /* No helper is forcibly unloaded. It must finish before normal playback
     * continues: cancellation makes the controller kill the target if it had
     * already installed conversion hooks. */
    SetEvent(stop_event);
    if (outcome != WAIT_OBJECT_0 + 1 && WaitForSingleObject(helper_process, 10000) != WAIT_OBJECT_0) {
        /* Unknown live converter state must never enter the playback engine.
         * The helper normally closes its job on Stop; this is the final guard
         * when it is unresponsive before acknowledging installation. */
        TerminateProcess(GetCurrentProcess(), ERROR_TIMEOUT);
        ExitProcess(ERROR_TIMEOUT);
    }
    return FALSE;
}

static int __cdecl hooked_execute(const void *args, void *app, void *sandbox) {
    prepare_command();
    return real_execute(args, app, sandbox);
}
static int __cdecl hooked_initialize(const void *args, const void *settings, void *app, void *sandbox) {
    if (InterlockedCompareExchange(&helper_started, 1, 0) == 0) {
        if (prepare_command()) {
            /* COM must not wait for CEF initialization while CEF waits for our
             * ready signal. This point runs outside DllMain/loader lock. */
            system_combase = LoadLibraryExW(L"combase.dll", NULL, LOAD_LIBRARY_SEARCH_SYSTEM32);
            if (!system_combase || !launch_helper()) InterlockedExchange(&command_ready, 0);
        }
        release_port();
    }
    return real_initialize(args, settings, app, sandbox);
}

static void **import_slot(HMODULE target, const char *library, const char *name) {
    BYTE *base = (BYTE *)target;
    if (!base) return NULL;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < sizeof(*dos) || dos->e_lfanew > 1048576) return NULL;
    IMAGE_NT_HEADERS64 *nt = (IMAGE_NT_HEADERS64 *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return NULL;
    DWORD size = nt->OptionalHeader.SizeOfImage;
    IMAGE_DATA_DIRECTORY dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir.VirtualAddress || dir.Size < sizeof(IMAGE_IMPORT_DESCRIPTOR) ||
        dir.VirtualAddress >= size || dir.Size > size - dir.VirtualAddress) return NULL;
    IMAGE_IMPORT_DESCRIPTOR *entries = (IMAGE_IMPORT_DESCRIPTOR *)(base + dir.VirtualAddress);
    for (DWORD i = 0; i < dir.Size / sizeof(*entries) && entries[i].Name; ++i) {
        IMAGE_IMPORT_DESCRIPTOR *entry = entries + i;
        if (entry->Name >= size || _stricmp((char *)(base + entry->Name), library)) continue;
        if (!entry->OriginalFirstThunk || !entry->FirstThunk) continue;
        for (DWORD n = 0; n < 65536; ++n) {
            ULONGLONG lookup_rva = (ULONGLONG)entry->OriginalFirstThunk + n * 8ULL;
            ULONGLONG slot_rva = (ULONGLONG)entry->FirstThunk + n * 8ULL;
            if (lookup_rva > size - 8 || slot_rva > size - 8) return NULL;
            ULONGLONG lookup = *(ULONGLONG *)(base + lookup_rva);
            if (!lookup) break;
            if (lookup & IMAGE_ORDINAL_FLAG64) continue;
            if (lookup > size - sizeof(IMAGE_IMPORT_BY_NAME)) return NULL;
            IMAGE_IMPORT_BY_NAME *symbol = (IMAGE_IMPORT_BY_NAME *)(base + lookup);
            if (!strcmp((char *)symbol->Name, name)) return (void **)(base + slot_rva);
        }
    }
    return NULL;
}
static BOOL replace_slot(void **slot, void *target) {
    DWORD old, ignored;
    if (!VirtualProtect(slot, sizeof(void *), PAGE_READWRITE, &old)) return FALSE;
    InterlockedExchangePointer(slot, target);
    VirtualProtect(slot, sizeof(void *), old, &ignored);
    return TRUE;
}
static BOOL install_hooks(void) {
    HMODULE music = GetModuleHandleW(L"cloudmusic.dll"), cef = GetModuleHandleW(L"libcef.dll");
    if (!music || !cef) return FALSE;
    void **execute_slot = import_slot(music, "libcef.dll", "cef_execute_process");
    void **initialize_slot = import_slot(music, "libcef.dll", "cef_initialize");
    void **command_w_slot = import_slot(cef, "kernel32.dll", "GetCommandLineW");
    void **command_a_slot = import_slot(cef, "kernel32.dll", "GetCommandLineA");
    if (!execute_slot || !initialize_slot || !command_w_slot || !command_a_slot ||
        *execute_slot != (void *)GetProcAddress(cef,"cef_execute_process") ||
        *initialize_slot != (void *)GetProcAddress(cef,"cef_initialize")) return FALSE;
    real_execute = (cef_execute_fn)*execute_slot;
    real_initialize = (cef_initialize_fn)*initialize_slot;
    real_command_w = (LPWSTR (WINAPI *)(void))*command_w_slot;
    real_command_a = (LPSTR (WINAPI *)(void))*command_a_slot;
    if (!replace_slot(command_w_slot, local_command_w)) return FALSE;
    if (!replace_slot(command_a_slot, local_command_a)) goto undo_w;
    if (!replace_slot(execute_slot, hooked_execute)) goto undo_a;
    if (!replace_slot(initialize_slot, hooked_initialize)) goto undo_execute;
    return TRUE;
undo_execute: replace_slot(execute_slot, real_execute);
undo_a: replace_slot(command_a_slot, real_command_a);
undo_w: replace_slot(command_w_slot, real_command_w);
    return FALSE;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
        DWORD count = GetModuleFileNameW(NULL, executable_path, 32768);
        if (!count || count >= 32768) return TRUE;
        const WCHAR *leaf = wcsrchr(executable_path,L'\\');
        if (!leaf || _wcsicmp(leaf+1,L"cloudmusic.exe") || wcsstr(GetCommandLineW(),L"--type=")) return TRUE;
        count = GetModuleFileNameW(instance,self_directory,32768);
        if (!count || count >= 32768) return TRUE;
        WCHAR *slash = wcsrchr(self_directory,L'\\');
        if (!slash) return TRUE; *slash = 0;
        install_hooks();
    }
    /* On process exit the OS closes these handles. No loader-lock waits or
     * detach of live audio hooks is performed from this DLL. */
    return TRUE;
}

static BOOL CALLBACK initialize_forwarding(PINIT_ONCE once, PVOID parameter, PVOID *context) {
    WCHAR path[32768];
    UINT count = GetSystemDirectoryW(path,32768);
    if (!count || count >= 32768-12) return TRUE;
    wcscat(path,L"\\msimg32.dll");
    system_msimg = LoadLibraryExW(path,NULL,LOAD_LIBRARY_SEARCH_SYSTEM32);
    const char *names[5] = {"AlphaBlend","DllInitialize","GradientFill","TransparentBlt","vSetDdrawflag"};
    if (system_msimg) for (unsigned n=0;n<5;++n) forwarding[n]=GetProcAddress(system_msimg,names[n]);
    return TRUE;
}
static intptr_t missing_export(void) { SetLastError(ERROR_PROC_NOT_FOUND); return 0; }
/* Assembly export thunks preserve every Win64 argument register and tail-jump
 * to the original function, so even undocumented export signatures survive. */
void *resolve_msimg(unsigned index) {
    InitOnceExecuteOnce(&forwarding_once,initialize_forwarding,NULL,NULL);
    return index<5 && forwarding[index] ? (void *)forwarding[index] : (void *)missing_export;
}
