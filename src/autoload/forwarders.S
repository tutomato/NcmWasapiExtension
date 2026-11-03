.text
.intel_syntax noprefix
.extern resolve_msimg
.macro FORWARD symbol, index
.globl \symbol
.def \symbol; .scl 2; .type 32; .endef
.seh_proc \symbol
\symbol:
    sub rsp, 168
    .seh_stackalloc 168
    .seh_endprologue
    mov [rsp + 32], rcx
    mov [rsp + 40], rdx
    mov [rsp + 48], r8
    mov [rsp + 56], r9
    movdqu [rsp + 64], xmm0
    movdqu [rsp + 80], xmm1
    movdqu [rsp + 96], xmm2
    movdqu [rsp + 112], xmm3
    mov ecx, \index
    call resolve_msimg
    mov [rsp + 128], rax
    movdqu xmm0, [rsp + 64]
    movdqu xmm1, [rsp + 80]
    movdqu xmm2, [rsp + 96]
    movdqu xmm3, [rsp + 112]
    mov rcx, [rsp + 32]
    mov rdx, [rsp + 40]
    mov r8, [rsp + 48]
    mov r9, [rsp + 56]
    mov rax, [rsp + 128]
    add rsp, 168
    jmp rax
.seh_endproc
.endm
FORWARD proxy_AlphaBlend, 0
FORWARD proxy_DllInitialize, 1
FORWARD proxy_GradientFill, 2
FORWARD proxy_TransparentBlt, 3
FORWARD proxy_vSetDdrawflag, 4
