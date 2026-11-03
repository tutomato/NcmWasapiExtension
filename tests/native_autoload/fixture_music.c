#define UNICODE
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
__declspec(dllimport) int cef_execute_process(const void *,void *,void *);
__declspec(dllimport) int cef_initialize(const void *,const void *,void *,void *);
__declspec(dllexport) int Run(void) {
    if (cef_execute_process(NULL,NULL,NULL)!=-1) return 21;
    if (!cef_initialize(NULL,NULL,NULL,NULL)) return 22;
    /* Compare forwarded eleven-argument AlphaBlend against System32 directly. */
    WCHAR path[MAX_PATH]; GetSystemDirectoryW(path,MAX_PATH); lstrcatW(path,L"\\msimg32.dll");
    HMODULE system=LoadLibraryW(path);
    if (!system) return 23;
    typedef BOOL (WINAPI *blend_fn)(HDC,int,int,int,int,HDC,int,int,int,int,BLENDFUNCTION);
    blend_fn direct=(blend_fn)GetProcAddress(system,"AlphaBlend");
    if (!direct) return 24;
    HDC source=CreateCompatibleDC(NULL),destination=CreateCompatibleDC(NULL);
    BITMAPINFO info={0}; info.bmiHeader.biSize=sizeof(BITMAPINFOHEADER);info.bmiHeader.biWidth=1;
    info.bmiHeader.biHeight=-1;info.bmiHeader.biPlanes=1;info.bmiHeader.biBitCount=32;info.bmiHeader.biCompression=BI_RGB;
    DWORD *src,*dst; HBITMAP a=CreateDIBSection(source,&info,DIB_RGB_COLORS,(void **)&src,NULL,0);
    HBITMAP b=CreateDIBSection(destination,&info,DIB_RGB_COLORS,(void **)&dst,NULL,0);
    if (!a || !b) return 25;
    HGDIOBJ olda=SelectObject(source,a),oldb=SelectObject(destination,b);
    *src=0x00dd3311;*dst=0x001155aa; BLENDFUNCTION blend={AC_SRC_OVER,0,128,0};
    BOOL first=direct(destination,0,0,1,1,source,0,0,1,1,blend); GdiFlush();DWORD expected=*dst;
    *dst=0x001155aa;
    BOOL second=AlphaBlend(destination,0,0,1,1,source,0,0,1,1,blend);GdiFlush();DWORD actual=*dst;
    SelectObject(source,olda);SelectObject(destination,oldb);DeleteObject(a);DeleteObject(b);
    DeleteDC(source);DeleteDC(destination);FreeLibrary(system);
    return first && second && expected==actual && actual!=0x001155aa ? 0 : 26;
}
