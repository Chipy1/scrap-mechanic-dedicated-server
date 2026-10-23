FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG GE_PROTON_VERSION=GE-Proton11-11
ARG GE_PROTON_FLAVOR=x86_64

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/steam \
    USER=steam \
    DISPLAY=:99 \
    STEAM_ROOT=/home/steam/.local/share/Steam \
    GE_PROTON_DIR=/opt/proton/current \
    GE_PROTON_NAME=${GE_PROTON_VERSION}-${GE_PROTON_FLAVOR} \
    SM_SAVES_ROOT="/home/steam/.local/share/Steam/steamapps/compatdata/387990/pfx/drive_c/users/steamuser/AppData/Roaming/Axolot Games/Scrap Mechanic/User" \
    SM_LOGS_ROOT="/home/steam/.local/share/Steam/steamapps/common/Scrap Mechanic/Logs" \
    SM_SEED_DIR=/seed \
    LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=llvmpipe \
    MESA_LOADER_DRIVER_OVERRIDE=llvmpipe \
    DXVK_FILTER_DEVICE_NAME=llvmpipe \
    DXVK_CONFIG_FILE=/usr/local/lib/sm/dxvk.conf \
    PROTON_NO_ESYNC=1 \
    PROTON_NO_FSYNC=1 \
    WINEDEBUG=-all

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg2 tar gzip xz-utils unzip file lsof \
        xvfb xauth x11-utils x11-xserver-utils openbox xdotool wmctrl \
        dbus-x11 at-spi2-core \
        vulkan-tools mesa-utils \
        libvulkan1 libvulkan1:i386 mesa-vulkan-drivers mesa-vulkan-drivers:i386 \
        libgl1 libgl1:i386 libglx-mesa0 libglx-mesa0:i386 libegl1 libegl1:i386 \
        libgl1-mesa-dri libgl1-mesa-dri:i386 libgbm1 \
        libc6-i386 lib32gcc-s1 lib32stdc++6 libtinfo6:i386 \
        python3-minimal python3 \
        gosu tini procps psmisc \
        fonts-liberation fontconfig \
        libnss3 libnspr4 libxss1 libxtst6 libxrandr2 libxcomposite1 \
        libxdamage1 libxfixes3 libxkbcommon0 libxi6 libxcursor1 libxinerama1 \
        libgtk-3-0t64 libgdk-pixbuf-2.0-0 libpango-1.0-0 libcairo2 libasound2t64 \
        libpulse0 libcups2t64 libdrm2 libatspi2.0-0t64 libsecret-1-0 \
        libayatana-appindicator3-1 \
        libdbus-1-3 libudev1 libglib2.0-0 libsm6 libice6 libxext6 libxrender1 \
        zenity xdg-user-dirs xterm \
    && rm -rf /var/lib/apt/lists/*

# The Steam launcher package adds Valve's apt repository. Install the runtime
# libraries explicitly so the client never tries to bootstrap them at runtime.
RUN curl -fsSL --retry 3 -o /tmp/steam.deb \
        https://cdn.akamai.steamstatic.com/client/installer/steam.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/steam.deb \
    && rm -f /tmp/steam.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends steam-libs-amd64 steam-libs-i386 \
    && rm -rf /var/lib/apt/lists/* \
    && mv /usr/bin/steamdeps /usr/bin/steamdeps.real \
    && printf '#!/bin/sh\nexit 0\n' > /usr/bin/steamdeps \
    && chmod 0755 /usr/bin/steamdeps

RUN mkdir -p /opt/steamcmd \
    && curl -fsSL --retry 3 -o /tmp/steamcmd.tar.gz \
        https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz \
    && tar -xzf /tmp/steamcmd.tar.gz -C /opt/steamcmd \
    && rm -f /tmp/steamcmd.tar.gz \
    && chmod 0755 /opt/steamcmd/steamcmd.sh /opt/steamcmd/linux32/steamcmd \
    && chown -R 1000:1000 /opt/steamcmd \
    && printf '#!/bin/sh\nexec /opt/steamcmd/steamcmd.sh "$@"\n' > /usr/local/bin/steamcmd \
    && chmod 0755 /usr/local/bin/steamcmd

RUN mkdir -p /opt/proton \
    && curl -fsSL --retry 3 -o /tmp/ge-proton.tar.gz \
        "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/${GE_PROTON_VERSION}/${GE_PROTON_VERSION}-${GE_PROTON_FLAVOR}.tar.gz" \
    && tar -xzf /tmp/ge-proton.tar.gz -C /opt/proton \
    && rm -f /tmp/ge-proton.tar.gz \
    && ln -sfn "/opt/proton/${GE_PROTON_VERSION}-${GE_PROTON_FLAVOR}" /opt/proton/current

RUN useradd --uid 1000 --non-unique --create-home --shell /bin/bash steam \
    && mkdir -p /home/steam/.local/share/Steam /home/steam/.cache/sm /seed \
    && chown -R steam:steam /home/steam /seed

COPY entrypoint.sh /entrypoint.sh
COPY entrypoint-user.sh /usr/local/lib/sm/entrypoint-user.sh
COPY validate_save.py /usr/local/lib/sm/validate_save.py
COPY steam_id.py /usr/local/lib/sm/steam_id.py
COPY patch_steam_config.py /usr/local/lib/sm/patch_steam_config.py
COPY accept_eula.py /usr/local/lib/sm/accept_eula.py
COPY dxvk.conf /usr/local/lib/sm/dxvk.conf
RUN chmod 0755 /entrypoint.sh /usr/local/lib/sm/entrypoint-user.sh \
        /usr/local/lib/sm/validate_save.py /usr/local/lib/sm/steam_id.py \
        /usr/local/lib/sm/patch_steam_config.py /usr/local/lib/sm/accept_eula.py

USER root
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["start"]
