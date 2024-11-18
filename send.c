#include <stdlib.h>
#include <stdio.h>

int main(int argc, char **argv)
{
    char *option = "";
    if (argc > 1) {
        option = argv[1];
    }

    char buf[1024];
    snprintf(buf, sizeof(buf), "python F:\\Workspaces\\Python\\wallpaper\\wallpaper.py --csend %s", option);
    return system(buf);
}
