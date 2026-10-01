#define _GNU_SOURCE
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int launcher_main(int install_mode, int argc, char **argv) {
    char exe[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (n < 0 || n >= (ssize_t)sizeof(exe) - 1) {
        perror("Clock: readlink /proc/self/exe");
        return 1;
    }
    exe[n] = '\0';
    char *slash = strrchr(exe, '/');
    if (!slash) {
        fprintf(stderr, "Clock: invalid executable path\n");
        return 1;
    }
    *slash = '\0';

    char main_py[PATH_MAX];
    int written = snprintf(main_py, sizeof(main_py), "%s/main.py", exe);
    if (written < 0 || (size_t)written >= sizeof(main_py)) {
        fprintf(stderr, "Clock: path too long\n");
        return 1;
    }

    char **child = calloc((size_t)argc + (install_mode ? 2 : 1), sizeof(char *));
    if (!child) {
        perror("Clock: calloc");
        return 1;
    }

    int j = 0;
    child[j++] = "python3";
    child[j++] = main_py;
    if (install_mode) child[j++] = "--clockinstall";
    for (int i = 1; i < argc; ++i) child[j++] = argv[i];
    child[j] = NULL;

    execvp(child[0], child);
    fprintf(stderr, "Clock: could not start python3: %s\n", strerror(errno));
    free(child);
    return 127;
}

#ifndef CLOCKINSTALL
int main(int argc, char **argv) { return launcher_main(0, argc, argv); }
#else
int main(int argc, char **argv) { return launcher_main(1, argc, argv); }
#endif
