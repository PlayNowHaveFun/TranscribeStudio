// Mach-O launcher for Transcribe Studio.app.
//
// macOS 15+ stopped reliably propagating TCC grants (Full Disk Access,
// Files & Folders) to .app bundles whose CFBundleExecutable is a shell
// script — the script interpreter's "responsible code identifier" can't
// be anchored to the .app's bundle identity, so the user's FDA toggle
// applies to nothing. The workaround is to make the bundle's executable
// a real signed Mach-O binary; macOS then anchors TCC to *this* binary,
// and child processes (bash, python) inherit the bundle's RCI through
// execv() and keep the grant.
//
// All this binary does: find launcher.sh sitting next to it inside
// Contents/Resources, and execv into bash with that script. The script
// can then do whatever orchestration it likes (port check, browser
// open, exec run.sh) under the .app's TCC scope.
//
// Compile:
//   clang -O2 -o "Transcribe Studio.app/Contents/MacOS/TranscribeStudio" \
//         bin/launcher.c

#include <libgen.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(void) {
    char exe[4096];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) {
        fprintf(stderr, "TranscribeStudio: _NSGetExecutablePath failed\n");
        return 1;
    }

    // exe = ".../Transcribe Studio.app/Contents/MacOS/TranscribeStudio"
    // walk up twice to get .../Contents, then descend into Resources/launcher.sh.
    // dirname() mutates its argument, so make a copy first.
    char buf[4096];
    snprintf(buf, sizeof(buf), "%s", exe);
    char *contents = dirname(dirname(buf));
    char script[4096];
    snprintf(script, sizeof(script), "%s/Resources/launcher.sh", contents);

    char *argv[] = {"/bin/bash", script, NULL};
    execv("/bin/bash", argv);
    perror("TranscribeStudio: execv /bin/bash");
    return 1;
}
