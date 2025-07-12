#!/usr/bin/python3
# -*- coding: utf-8 -*-
import os,logging,tempfile,subprocess,re,json,argparse,json,hashlib,time
from datetime import datetime

import json5 # dev-python/json5
import requests # dev-python/requests

DEFAULT_LOWER_SIZE_IN_GIB = 24  # Default max size of lower image in GiB
OVERLAY_SOURCE = "https://github.com/wbrxcorp/genpack-overlay.git"

arch = os.uname().machine

work_root = "work"
work_dir = os.path.join(work_root, arch)

cache_root = os.path.join(os.path.expanduser("~"), ".cache/genpack")
cache_arch_dir = os.path.join(cache_root, arch)
binpkgs_dir = os.path.join(cache_arch_dir, "binpkgs")
download_dir = os.path.join(cache_root, "download")

base_url = "http://ftp.iij.ad.jp/pub/linux/gentoo/"
user_agent = "genpack/0.1"
overlay_override = None
independent_binpkgs = False
deep_depclean = False
genpack_json = None
genpack_json_time = None

mixin_root = os.path.join(work_root, "mixins")
mixins = []
mixin_genpack_json = {}

class Variant:
    def __init__(self, name):
        self.name = name
        self.lower_image = os.path.join(work_dir, "lower.img") if self.name is None else os.path.join(work_dir, "lower-%s.img" % self.name)
        self.lower_files = os.path.join(work_dir, "lower.files") if self.name is None else os.path.join(work_dir, "lower-%s.files" % self.name)
        self.upper_dir = os.path.join(work_dir, "upper") if self.name is None else os.path.join(work_dir, "upper-%s" % self.name)

def sudo(cmd):
    # if current user is root, just return the command
    if os.geteuid() == 0:
        return cmd
    #else
    return ['sudo'] + cmd

def url_readlines(url):
    """Read lines from a URL."""
    logging.debug(f"Reading lines from URL: {url}")
    headers = {'User-Agent': user_agent}
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # Raise an error for bad responses
    lines = response.text.splitlines()
    logging.debug(f"Read {len(lines)} lines from {url}")
    return lines

def get_latest_stage3_tarball_url(stage3_variant = "systemd"):
    _arch = arch
    _arch2 = arch
    if _arch == "x86_64": _arch = _arch2 = "amd64"
    elif _arch == "i686": _arch = "x86"
    elif _arch == "aarch64": _arch = _arch2 = "arm64"
    elif _arch == "riscv64":
        _arch = "riscv"
        _arch2 = "rv64_lp64d"
    current_status = None
    for line in url_readlines(base_url + "releases/" + _arch + "/autobuilds/latest-stage3-" + _arch2 + "-%s.txt" % (stage3_variant,)):
        if current_status is None:
            if line == "-----BEGIN PGP SIGNED MESSAGE-----": current_status = "header"
            continue
        elif current_status == "header":
            if line == "": current_status = "body"
            continue
        elif current_status == "body":
            if line == "-----BEGIN PGP SIGNATURE-----": break
            line = re.sub(r'#.*$', "", line.strip())
            if line == "": continue
            #else
            splitted = line.split(" ")
            if len(splitted) < 2: continue
            #else
            return base_url + "releases/" + _arch + "/autobuilds/" + splitted[0]
    #else
    raise Exception("No stage3 tarball (arch=%s,stage3_variant=%s) found", arch, stage3_variant)

def get_latest_portage_tarball_url():
    return base_url + "snapshots/portage-latest.tar.xz"

def headers_to_info(headers):
    return f"Last-Modified:{headers.get('Last-Modified', '')} ETag:{headers.get('ETag', '')} Content-Length:{headers.get('Content-Length', '')}"

def get_headers(url):
    """Get the headers of a URL."""
    logging.debug(f"Getting headers for URL: {url}")
    headers = {'User-Agent': user_agent}
    response = requests.head(url, headers=headers)
    response.raise_for_status()  # Raise an error for bad responses
    logging.debug(f"Headers for {url}: {response.headers}")
    return response.headers

def download(url, dest):
    headers = {'User-Agent': user_agent}
    response = requests.get(url, stream=True, headers=headers)
    response.raise_for_status()  # Raise an error for bad responses

    with open(dest, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    logging.info(f"Downloaded {url} to {dest}")
    return response.headers

class TempMount:
    def __init__(self, image_path):
        self.image_path = image_path
        logging.debug(f"Initializing TempMount with image: {self.image_path}")
        self.mount_point = tempfile.mkdtemp(prefix="genpack_mount_")
        logging.debug(f"Temporary mount point created: {self.mount_point}")

    def __enter__(self):
        # Logic to mount the filesystem
        logging.debug(f"Mounting to {self.mount_point}")
        subprocess.run(sudo(['mount', self.image_path, self.mount_point]), check=True)
        logging.debug(f"Mounted {self.image_path} at {self.mount_point}")
        return self.mount_point

    def __exit__(self, exc_type, exc_value, traceback):
        # Logic to unmount the filesystem
        logging.debug(f"Unmounting {self.mount_point}")
        subprocess.run(sudo(['umount', self.mount_point]), check=True)
        # Clean up the temporary mount point
        os.rmdir(self.mount_point)
        logging.debug(f"Temporary mount point removed: {self.mount_point}")

def setup_lower_image(lower_image, stage3_tarball, portage_tarball):
    # create image file
    lower_size_in_gib = genpack_json.get("lower-layer-capacity", DEFAULT_LOWER_SIZE_IN_GIB)
    logging.info(f"Creating image file at {lower_image} with size {lower_size_in_gib} GiB.")
    with open(lower_image, "wb") as f:
        f.seek(lower_size_in_gib * 1024 * 1024 * 1024 - 1)
        f.write(b'\x00')
    try:
        logging.info(f"Formatting filesystem on {lower_image}")
        subprocess.run(['mkfs.ext4', lower_image], check=True)
        logging.info("Filesystem formatted successfully.")
        with TempMount(lower_image) as mount_point:
            logging.info("Extracting stage3 to lower image...")
            subprocess.run(sudo(['tar', 'xpf', stage3_tarball, '-C', mount_point]), check=True)
            logging.info("Extracting portage to lower image...")
            portage_dir = os.path.join(mount_point, "var/db/repos/gentoo")
            subprocess.run(sudo(["mkdir", "-p", portage_dir]), check=True)
            subprocess.run(sudo(['tar', 'xpf', portage_tarball, '-C', portage_dir, "--strip-components=1"]), check=True)
            # workaround for https://bugs.gentoo.org/734000
            subprocess.run(sudo(['chroot', mount_point, "chown", "portage", "/var/cache/distfiles"]), check=True)
            subprocess.run(sudo(['chroot', mount_point, "chmod", "g+w", "/var/cache/distfiles"]), check=True)
    except Exception as e:
        logging.error(f"Error setting up lower image: {e}")
        os.remove(lower_image)  # Clean up the image
        raise

def replace_portage(lower_image, portage_tarball):
    logging.info(f"Replacing portage in lower image: {lower_image}")
    with TempMount(lower_image) as mount_point:
        portage_dir = os.path.join(mount_point, "var/db/repos/gentoo")
        if os.path.exists(portage_dir):
            # rename the old portage directory to uniqe name using timestamp
            old_portage_dir = portage_dir + ".old-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            logging.info(f"Renaming old portage directory to {old_portage_dir}")
            subprocess.run(sudo(['mv', portage_dir, old_portage_dir]), check=True)
        subprocess.run(sudo(["mkdir", "-p", portage_dir]), check=True)
        subprocess.run(sudo(['tar', 'xpf', portage_tarball, '-C', portage_dir, "--strip-components=1"]), check=True)
        logging.info("Portage replaced successfully.")

def lower_exec(lower_image, cmdline, env=None):
    if isinstance(cmdline, str):
        cmdline = [cmdline]

    if env is None:
        env = {}

    if os.environ.get("TERM", None) == "xterm-ghostty" and "TERM" not in env:
        env["TERM"] = "xterm-256color"
    
    # use PID for container name
    container_name = "genpack-%d" % os.getpid()
    nspawn_cmdline = ["systemd-nspawn", "-q", "--suppress-sync=true", 
        "--as-pid2", "-M", container_name, f"--image={lower_image}",
        "--tmpfs=/var/tmp",
        "--capability=CAP_MKNOD,CAP_SYS_ADMIN,CAP_NET_ADMIN", # Portage's network sandbox needs CAP_NET_ADMIN
    ]
    if not independent_binpkgs:
        os.makedirs(binpkgs_dir, exist_ok=True)
        nspawn_cmdline.append(f"--bind={binpkgs_dir}:/var/cache/binpkgs{':rootidmap' if os.geteuid() != 0 else ''}")
    if overlay_override is not None:
        if not os.path.isdir(overlay_override):
            raise ValueError("overlay-override must be a directory")
        #else
        nspawn_cmdline.append(f"--bind={os.path.abspath(overlay_override)}:/var/db/repos/genpack-overlay")
    if env is not None:
        if not isinstance(env, dict):
            raise ValueError("env must be a dictionary")
        #else
        for k, v in env.items():
            nspawn_cmdline.append(f"--setenv={k}={v}")
    nspawn_cmdline += cmdline

    subprocess.run(sudo(nspawn_cmdline), check=True)

def escape_colon(s):
    # systemd-nspawn's some options need colon to be escaped
    return re.sub(r':', r'\:', s)

def upper_exec(variant, cmdline, user=None):
    # convert command to list if it is string
    if isinstance(cmdline, str): cmdline = [cmdline]
    container_name = "genpack-%d" % os.getpid()
    os.makedirs(download_dir, exist_ok=True)
    nspawn_cmdline = ["systemd-nspawn", "-q", "--suppress-sync=true", 
        "--as-pid2", "-M", container_name, 
        f"--image={variant.lower_image}", "--overlay=+/:%s:/" % escape_colon(os.path.abspath(variant.upper_dir)),
        f"--bind={os.path.abspath(download_dir)}:/var/cache/download{':rootidmap' if os.geteuid() != 0 else ''}",
        "--capability=CAP_MKNOD,CAP_NET_ADMIN",
        "-E", f"ARTIFACT={genpack_json["name"]}"
    ]
    if variant.name is not None:
        nspawn_cmdline += ["-E", f"VARIANT={variant.name}"]

    if os.environ.get("TERM", None) == "xterm-ghostty":
        nspawn_cmdline += ["-E", "TERM=xterm-256color"]

    if user is not None:
        if not isinstance(user, str):
            raise ValueError("user must be a string")
        #else
        nspawn_cmdline.append(f"--user={user}")
    subprocess.check_call(sudo(nspawn_cmdline + cmdline))

def download_mixins():
    global mixins, mixin_genpack_json
    mixins_tmp = genpack_json.get("mixin", None)
    if mixins_tmp is None:
        return
    #else

    if isinstance(mixins_tmp, str):
        mixins_tmp = [mixins_tmp]
    elif not isinstance(mixins_tmp, list):
        raise ValueError("mixins must be a string or a list of strings")

    if not os.path.isdir(mixin_root):
        os.makedirs(mixin_root, exist_ok=True)

    for mixin in mixins_tmp:
        if not isinstance(mixin, str):
            raise ValueError("mixin must be a string")
        #else
        # treat sha-256 hash of mixin name as an identifier
        mixin_id = hashlib.sha256(mixin.encode('utf-8')).hexdigest()
        mixin_dir = os.path.join(mixin_root, mixin_id)
        if os.path.isdir(mixin_dir):
            # perform git pull
            logging.info(f"Mixin {mixin} already exists, updating...")
            try:
                subprocess.run(['git', '-C', mixin_dir, 'pull'], check=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to update mix-in {mixin}({mixin_id}). Proceeding without updating.  If you need to reset mix-ins, remove the directory {mixin_dir} and try again.")
                continue
        else:
            # perform git clone
            logging.info(f"Downloading mix-in {mixin}...")
            subprocess.run(['git', 'clone', mixin, mixin_dir], check=True)
        mixin_genpack_json[mixin_id] = load_genpack_json(mixin_dir)
        mixins.append(mixin_id)

def sync_genpack_overlay(lower_image):
    with TempMount(lower_image) as mount_point:
        genpack_overlay_dir = os.path.join(mount_point, "var/db/repos/genpack-overlay")
        if os.path.isdir(genpack_overlay_dir):
            subprocess.run(sudo(['git', '-C', genpack_overlay_dir, 'pull']), check=True)
        else:
            logging.info("Genpack overlay not found, cloning...")
            subprocess.run(sudo(['git', 'clone', OVERLAY_SOURCE, genpack_overlay_dir]), check=True)
        repos_conf = os.path.join(mount_point, "etc/portage/repos.conf/genpack-overlay.conf")
        if not os.path.isfile(repos_conf):
            logging.info("Creating repos.conf for genpack-overlay")
            subprocess.run(sudo(['mkdir', '-p', os.path.dirname(repos_conf)]), check=True)
            tee = subprocess.Popen(sudo(['tee', repos_conf]), stdin=subprocess.PIPE, text=True)
            tee.stdin.write("[genpack-overlay]\nlocation=/var/db/repos/genpack-overlay")
            tee.stdin.close()
            tee.wait()
        accept_keywords_file = os.path.join(mount_point, "etc/portage/package.accept_keywords/genpack")
        if not os.path.isfile(accept_keywords_file):
            logging.info("Creating package.accept_keywords for genpack")
            subprocess.run(sudo(['mkdir', '-p', os.path.dirname(accept_keywords_file)]), check=True)
            tee = subprocess.Popen(sudo(['tee', accept_keywords_file]), stdin=subprocess.PIPE, text=True)
            tee.stdin.write("dev-cpp/argparse\n")
            tee.stdin.close()
            tee.wait()
        use_file = os.path.join(mount_point, "etc/portage/package.use/genpack")
        if not os.path.isfile(use_file):
            logging.info("Creating package.use for genpack")
            subprocess.run(sudo(['mkdir', '-p', os.path.dirname(use_file)]), check=True)
            tee = subprocess.Popen(sudo(['tee', use_file]), stdin=subprocess.PIPE, text=True)
            tee.stdin.write("sys-kernel/installkernel dracut\n")
            tee.stdin.close()
            tee.wait()
        overlay_checkfiles = os.listdir(genpack_overlay_dir)
        overlay_checkfiles.remove(".git")
        overlay_checkfiles.append(".git/ORIG_HEAD")
        return get_latest_mtime(*(os.path.join(genpack_overlay_dir, f) for f in overlay_checkfiles if os.path.exists(os.path.join(genpack_overlay_dir, f))))

def apply_portage_sets_and_flags(lower_image, runtime_packages, buildtime_packages, devel_packages, accept_keywords, use, license, mask):
    with TempMount(lower_image) as mount_point:
        if accept_keywords is None: accept_keywords = {}
        if use is None: use = {}
        if license is None: license = {}
        if mask is None: mask = []
        if buildtime_packages is None: buildtime_packages = []

        etc_dir = os.path.join(mount_point, "etc")
        etc_portage_dir = os.path.join(etc_dir, "portage")

        if not isinstance(runtime_packages, list):
            raise ValueError("runtime_packages must be a list")
        #else

        sets_dir = os.path.join(etc_portage_dir, "sets")

        runtime_packages_file = os.path.join(sets_dir, "genpack-runtime")
        subprocess.run(sudo(["chroot", mount_point, "mkdir", "-p", "/etc/portage/sets"]), check=True)
        logging.info(f"Applying runtime packages to {runtime_packages_file}")
        tee = subprocess.Popen(sudo(['tee', runtime_packages_file]), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        try:
            for pkg in runtime_packages:
                tee.stdin.write(f"{pkg}\n")
        finally:
            tee.stdin.close()
            tee.wait()
        
        if not isinstance(buildtime_packages, list):
            raise ValueError("buildtime_packages must be a list or None")
        #else
        buildtime_packages_file = os.path.join(sets_dir, "genpack-buildtime")
        logging.info(f"Applying buildtime packages to {buildtime_packages_file}")
        tee = subprocess.Popen(sudo(['tee', buildtime_packages_file]), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        try:
            for pkg in buildtime_packages:
                tee.stdin.write(f"{pkg}\n")
        finally:
            tee.stdin.close()
            tee.wait()

        if devel_packages is not None:        
            if not isinstance(devel_packages, list):
                raise ValueError("devel_packages must be a list or None")
            #else
            devel_packages_file = os.path.join(sets_dir, "genpack-devel")
            logging.info(f"Applying devel packages to {devel_packages_file}")
            tee = subprocess.Popen(sudo(['tee', devel_packages_file]), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
            try:
                for pkg in devel_packages:
                    tee.stdin.write(f"{pkg}\n")
            finally:
                tee.stdin.close()
                tee.wait()
        else:
            subprocess.run(sudo(['rm', '-f', os.path.join(sets_dir, "genpack-devel")]), check=True)

        if not isinstance(accept_keywords, dict):
            raise ValueError("accept_keywords must be a dictionary")
        accept_keywords_file = os.path.join(etc_portage_dir, "package.accept_keywords/genpack")
        logging.info(f"Applying accept keywords to {accept_keywords_file}")
        subprocess.run(sudo(['mkdir', '-p', os.path.dirname(accept_keywords_file)]), check=True)
        tee = subprocess.Popen(sudo(['tee', accept_keywords_file]), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        try:
            for k, v in accept_keywords.items():
                if v is None:
                    tee.stdin.write(f"{k}\n")
                elif isinstance(v, list):
                    tee.stdin.write(f"{k} {' '.join(v)}\n")
                else:
                    tee.stdin.write(f"{k} {v}\n")
        finally:
            tee.stdin.close()
            tee.wait()

        if not isinstance(use, dict):
            raise ValueError("use must be a dictionary")
        use_file = os.path.join(etc_portage_dir, "package.use/genpack")
        logging.info(f"Applying USE flags to {use_file}")
        subprocess.run(sudo(['mkdir', '-p', os.path.dirname(use_file)]), check=True)
        tee = subprocess.Popen(sudo(['tee', use_file]), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        try:
            for k, v in use.items():
                if v is None:
                    tee.stdin.write(f"{k}\n")
                elif isinstance(v, list):
                    tee.stdin.write(f"{k} {' '.join(v)}\n")
                else:
                    tee.stdin.write(f"{k} {v}\n")
        finally:
            tee.stdin.close()
            tee.wait()

        if not isinstance(license, dict):
            raise ValueError("license must be a dictionary")
        license_file = os.path.join(etc_portage_dir, "package.license/genpack")
        logging.info(f"Applying LICENSE flags to {license_file}")
        subprocess.run(sudo(['mkdir', '-p', os.path.dirname(license_file)]), check=True)
        tee = subprocess.Popen(sudo(['tee', license_file]), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        try:
            for k, v in license.items():
                if v is None:
                    tee.stdin.write(f"{k}\n")
                elif isinstance(v, list):
                    tee.stdin.write(f"{k} {' '.join(v)}\n")
                else:
                    tee.stdin.write(f"{k} {v}\n")
        finally:
            tee.stdin.close()
            tee.wait()

        if not isinstance(mask, list):
            raise ValueError("mask must be a list")
        mask_file = os.path.join(etc_portage_dir, "package.mask/genpack")
        logging.info(f"Applying masked packages to {mask_file}")
        subprocess.run(sudo(['mkdir', '-p', os.path.dirname(mask_file)]), check=True)
        tee = subprocess.Popen(sudo(['tee', mask_file]), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        try:
            for pkg in mask:
                tee.stdin.write(f"{pkg}\n")
        finally:
            tee.stdin.close()
            tee.wait()

        # apply savedconfig
        savedconfig_dir = os.path.join(etc_portage_dir, "savedconfig")
        if os.path.isdir("savedconfig"):
            logging.info(f"Installing savedconfig...")
            subprocess.run(sudo(['rsync', '-rlptD', "--delete", "savedconfig", etc_portage_dir]), check=True)
        elif os.path.exists(savedconfig_dir):
            logging.info(f"Removing existing savedconfig directory {savedconfig_dir}")
            subprocess.run(sudo(['rm', '-rf', savedconfig_dir]), check=True)

        # apply patches
        patches_dir = os.path.join(etc_portage_dir, "patches")
        if os.path.isdir("patches"):
            logging.info(f"Installing patches...")
            subprocess.run(sudo(['rsync', '-rlptD', "--delete", "patches", etc_portage_dir]), check=True)
        elif os.path.exists(patches_dir):
            logging.info(f"Removing existing patches directory {patches_dir}")
            subprocess.run(sudo(['rm', '-rf', patches_dir]), check=True)
        
        # apply kernel config
        kernel_dir = os.path.join(etc_dir, "kernel")
        if os.path.isdir("kernel"):
            logging.info(f"Installing kernel config...")
            subprocess.run(sudo(['rsync', '-rlptD', "--delete", "kernel", etc_dir]), check=True)
        elif os.path.exists(kernel_dir):
            logging.info(f"Removing existing kernel directory {kernel_dir}")
            subprocess.run(sudo(['rm', '-rf', kernel_dir]), check=True)

def set_gentoo_profile(lower_image, profile_name):
    with TempMount(lower_image) as mount_point:
        portage_dir = os.path.join(mount_point, "var/db/repos/gentoo")
        profiles_default_linux_dir = os.path.join(portage_dir, "profiles/default/linux")
        if not os.path.isdir(profiles_default_linux_dir):
            raise Exception(f"Portage directory {portage_dir} does not contain profiles/default/linux directory.")
        arch_map = {
            "alpha": ("alpha",),
            "x86_64": ("amd64",),
            "aarch64": ("arm64",),
            "ppc": ("ppc",),
            "ppc64": ("ppc64",),
            "ppc64le": ("pc64le",),
            "i686": ("x86","i686"),
            "riscv64": ("riscv","rv64/lp64d"),
            "loong": ("loong", "la64v100/lp64d"),
        }
        portage_arch = arch_map.get(arch, None)
        if portage_arch is None:
            raise Exception(f"Unsupported architecture: {arch}. Please add it to arch_map in {__file__}.")
        arch_dir = os.path.join(profiles_default_linux_dir, portage_arch[0])
        if not os.path.isdir(arch_dir):
            raise Exception(f"Portage directory {portage_dir} does not contain profiles/default/linux/{portage_arch[0]} directory.")
        # enum subdirectories in arch_dir
        subdirs = [float(d) for d in os.listdir(arch_dir) if os.path.isdir(os.path.join(arch_dir, d))]
        if len(subdirs) == 0:
            raise Exception(f"Portage directory {portage_dir} does not contain any subdirectories in profiles/default/linux/{portage_arch[0]} directory.")
        #else
        #pick the latest subdirectory
        latest_subdir = max(subdirs)
        latest_profile_dir = os.path.join(arch_dir, str(latest_subdir))
        exact_profile_dir = os.path.join(latest_profile_dir, portage_arch[1] if len(portage_arch) > 1 else "", profile_name)
        if not os.path.isdir(exact_profile_dir):
            raise Exception(f"Portage directory {portage_dir} does not contain profiles/default/linux/{portage_arch[0]}/{latest_subdir}/{profile_name} directory.")
        #else
        exact_profile = os.path.join(f"default/linux/{portage_arch[0]}/{latest_subdir}", portage_arch[1] if len(portage_arch) > 1 else "", profile_name)
        logging.info(f"Setting Gentoo profile to {exact_profile} in {mount_point}")
        subprocess.run(sudo(['chroot', mount_point, "eselect", "profile", "set", exact_profile]), check=True)
        logging.info(f"Gentoo profile set to {exact_profile} successfully.")

def load_genpack_json(directory="."):
    json_parser, json_file = None, None

    genpack_json5 = os.path.join(directory, "genpack.json5")
    if os.path.isfile(genpack_json5):
        json_parser = json5
        json_file = genpack_json5
    genpack_json = os.path.join(directory, "genpack.json")
    if os.path.isfile(genpack_json):
        if json_parser is None:
            json_parser = json
            json_file = genpack_json
        else:
            raise ValueError("Both genpack.json5 and genpack.json found. Please remove one of them.")
    if json_file is None:
        raise FileNotFoundError("""Neither genpack.json5 nor genpack.json file found. `echo '{"packages":["genpack/paravirt"]}' > genpack.json5` or so to create the minimal one.""")

    #else
    return (json_parser.load(open(json_file, "r")), os.path.getmtime(json_file))

def merge_genpack_json(trunk, branch, path, allowed_properties = ["outfile","devel","packages","buildtime_packages","devel_packages",
                                                           "accept_keywords","use","mask","license","binpkg_excludes","users","groups", 
                                                           "services", "arch","variants", ], variant = None):
    if not isinstance(trunk, dict):
        raise ValueError("trunk must be a dictionary")
    #else
    path_str = " > ".join(path)
    if not isinstance(branch, dict):
        raise ValueError(f"branch at {path_str} must be a dictionary")

    if "outfile" in allowed_properties and "outfile" in branch:
        trunk["outfile"] = branch["outfile"]
    
    if "devel" in allowed_properties and "devel" in branch:
        if not isinstance(branch["devel"], bool):
            raise ValueError(f"devel at {path_str} must be a boolean")
        #else
        trunk["devel"] = branch["devel"]

    if "packages" in allowed_properties and "packages" in branch:
        if not isinstance(branch["packages"], list):
            raise ValueError(f"packages at {path_str} must be a list")
        #else
        if "packages" not in trunk: trunk["packages"] = []
        for package in branch["packages"]:
            if package[0] == '-':
                package = package[1:]
                if package in trunk["packages"]:
                    trunk["packages"].remove(package)
            elif package not in trunk["packages"]:
                trunk["packages"].append(package)

    if "buildtime_packages" in allowed_properties:
        if "buildtime-packages" in branch:
            raise ValueError(f"buildtime-packages at {path_str} is deprecated, use buildtime_packages instead")
        if "buildtime_packages" in branch:
            if not isinstance(branch["buildtime_packages"], list):
                raise ValueError(f"buildtime_packages at {path_str} must be a list")
            #else
            if "buildtime_packages" not in trunk: trunk["buildtime_packages"] = []
            for package in branch["buildtime_packages"]:
                if package not in trunk["buildtime_packages"]:
                    trunk["buildtime_packages"].append(package)

    if "devel_packages" in allowed_properties:
        if "devel-packages" in branch:
            raise ValueError(f"devel-packages at {path_str} is deprecated, use devel_packages instead")
        if "devel_packages" in branch:
            if not isinstance(branch["devel_packages"], list):
                raise ValueError(f"devel_packages at {path_str} must be a list")
            #else
            if "devel_packages" not in trunk: trunk["devel_packages"] = []
            for package in branch["devel_packages"]:
                if package not in trunk["devel_packages"]:
                    trunk["devel_packages"].append(package)
    
    if "accept_keywords" in allowed_properties and "accept_keywords" in branch:
        if not isinstance(branch["accept_keywords"], dict):
            raise ValueError(f"accept_keywords at {path_str} must be a dictionary")
        #else
        if "accept_keywords" not in trunk: trunk["accept_keywords"] = {}
        for k, v in branch["accept_keywords"].items():
            trunk["accept_keywords"][k] = v

    if "use" in allowed_properties and "use" in branch:
        if not isinstance(branch["use"], dict):
            raise ValueError(f"use at {path_str} must be a dictionary")
        #else
        if "use" not in trunk: trunk["use"] = {}
        for k, v in branch["use"].items():
            trunk["use"][k] = v # TODO: merge if already exists
    
    if "mask" in allowed_properties and "mask" in branch:
        if not isinstance(branch["mask"], list):
            raise ValueError(f"mask at {path_str} must be a list")
        #else
        if "mask" not in trunk: trunk["mask"] = []
        for package in branch["mask"]:
            if package not in trunk["mask"]:
                trunk["mask"].append(package)

    if "license" in allowed_properties and "license" in branch:
        if not isinstance(branch["license"], dict):
            raise ValueError(f"license at {path_str} must be a dictionary")
        #else
        if "license" not in trunk: trunk["license"] = {}
        for k, v in branch["license"].items():
            trunk["license"][k] = v
    
    if "binpkg_excludes" in allowed_properties:
        if "binpkg-exclude" in branch:
            raise ValueError(f"binpkg-exclude at {path_str} is deprecated, use binpkg_excludes instead")
        if "binpkg_excludes" in branch:
            if not isinstance(branch["binpkg_excludes"], (str, list)):
                raise ValueError(f"binpkg_excludes at {path_str} must be a string or a list of strings")
            #else
            if "binpkg_excludes" not in trunk: trunk["binpkg_excludes"] = []
            if isinstance(branch["binpkg_excludes"], str):
                branch["binpkg_excludes"] = [branch["binpkg_excludes"]]
            for package in branch["binpkg_excludes"]:
                if package not in trunk["binpkg_excludes"]:
                    trunk["binpkg_excludes"].append(package)

    if "users" in allowed_properties and "users" in branch:
        if not isinstance(branch["users"], list):
            raise ValueError(f"users at {path_str} must be a list")
        #else
        if "users" not in trunk: trunk["users"] = []
        trunk["users"] += branch["users"]

    if "groups" in allowed_properties and "groups" in branch:
        if not isinstance(branch["groups"], list):
            raise ValueError(f"groups at {path_str} must be a list")
        #else
        if "groups" not in trunk: trunk["groups"] = []
        trunk["groups"] += branch["groups"]
    
    if "services" in allowed_properties and "services" in branch:
        if not isinstance(branch["services"], list):
            raise ValueError(f"services at {path_str} must be a list")
        #else
        if "services" not in trunk: trunk["services"] = []
        for service in branch["services"]:
            if service not in trunk["services"]:
                trunk["services"].append(service)
    
    if "arch" in allowed_properties and "arch" in branch:
        if not isinstance(branch["arch"], dict):
            raise ValueError(f"arch at {path_str} must be a dictionary")
        #else
        for k, v in branch["arch"].items():
            if not isinstance(k, str):
                raise ValueError(f"arch at {path_str} must be a string")
            if arch in k.split('|'):
                merge_genpack_json(trunk, v, path + [f"arch={k}"], [
                    "packages","buildtime_packages","devel_packages",
                    "accept_keywords","use","mask","license","binpkg_exclude","services"
                ])
    
    if "variants" in allowed_properties and "variants" in branch and variant is not None:
        if not isinstance(branch["variants"], dict):    
            raise ValueError(f"variants at {path_str} must be a dictionary")
        #else
        if isinstance(variant, Variant): variant = variant.name
        if variant in branch["variants"]:
            merge_genpack_json(trunk, branch["variants"][variant], path + [f"variant={variant}"], [
                "name","outfile","packages","buildtime_packages","devel_packages",
                "accept_keywords","use","mask","license","binpkg_exclude","users","groups",
                "services","arch"
            ])

def lower(variant=None, devel=False):
    logging.info("Processing lower layer...")
    os.makedirs(work_dir, exist_ok=True)
    # todo: create .gitignore in work_root
    stage3_is_new = False
    stage3_url = get_latest_stage3_tarball_url()
    logging.info(f"Latest stage3 tarball URL: {stage3_url}")
    stage3_headers = get_headers(stage3_url)
    stage3_tarball = os.path.join(work_dir, "stage3.tar.xz")
    stage3_saved_headers_path = os.path.join(work_dir, "stage3.tar.xz.headers")
    stage3_saved_headers = open(stage3_saved_headers_path).read().strip() if os.path.isfile(stage3_saved_headers_path) else None
    if stage3_saved_headers != headers_to_info(stage3_headers):
        logging.info("Stage3 tarball info has changed, downloading new tarball.")
        stage3_headers = download(stage3_url, stage3_tarball)
        stage3_is_new = True
    
    portage_is_new = False
    portage_url = get_latest_portage_tarball_url()
    logging.info(f"Latest portage tarball URL: {portage_url}")
    portage_headers = get_headers(portage_url)
    portage_tarball = os.path.join(work_root, "portage.tar.xz") # because portage tarball is not architecture specific
    portage_saved_headers_path = os.path.join(work_root, "portage.tar.xz.headers")
    portage_saved_headers = open(portage_saved_headers_path).read().strip() if os.path.isfile(portage_saved_headers_path) else None
    if portage_saved_headers != headers_to_info(portage_headers):
        logging.info("Portage tarball info has changed, downloading new tarball.")
        portage_headers = download(portage_url, portage_tarball)
        portage_is_new = True

    image_is_new = False
    if stage3_is_new or not os.path.isfile(variant.lower_image):
        setup_lower_image(variant.lower_image, stage3_tarball, portage_tarball)
        image_is_new = True
        with open(stage3_saved_headers_path, 'w') as f:
            f.write(headers_to_info(stage3_headers))
    elif portage_is_new:
        replace_portage(variant.lower_image, portage_tarball)

    if portage_is_new:
        with open(portage_saved_headers_path, 'w') as f:
            f.write(headers_to_info(portage_headers))
    
    latest_mtime = sync_genpack_overlay(variant.lower_image)
    logging.debug(f"Latest genpack-overlay mtime: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_mtime))}")
    logging.debug(f"lower_files time: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(variant.lower_files))) if os.path.exists(variant.lower_files) else 'N/A'}")

    if os.path.exists(variant.lower_files) and (stage3_is_new or portage_is_new or os.path.getmtime(variant.lower_files) < latest_mtime):
        logging.info(f"Removing old {variant.lower_files} file due to changes in stage3 or portage.")
        os.remove(variant.lower_files)

    if os.path.exists(variant.lower_files):
        logging.info("Lower image is up-to-date, skipping.")
        return

    gentoo_profile = genpack_json.get("gentoo_profile", None)
    if gentoo_profile is not None and image_is_new:
        set_gentoo_profile(variant.lower_image, gentoo_profile)
    
    merged_genpack_json = {
        "accept_keywords": {
            "dev-cpp/argparse":None # argparse is required for genpack-progs
        },
        "use": {
            "sys-libs/glibc": "audit", # Intentionally causing glibc to be rebuilt
            "sys-kernel/installkernel":"dracut", # genpack depends on dracut
            "app-crypt/libb2":"-openmp", # openmp support brings gcc dependency, which is not generally needed for genpack
            "dev-lang/perl":"minimal",
            "app-editors/vim":"minimal"
        }
    }

    # merge mixins
    for mixin_id in mixins:
        if mixin_id in mixin_genpack_json:
            merge_genpack_json(merged_genpack_json, mixin_genpack_json[mixin_id], [f"mixin({mixin_id})"], [
                "packages","buildtime_packages","devel_packages","accept_keywords","use","mask",
                "license","binpkg_excludes","users","groups", "arch"
            ])

    # merge main genpack.json
    merge_genpack_json(merged_genpack_json, genpack_json, ["genpack.json"], 
        ["devel","packages","buildtime_packages","devel_packages",
            "accept_keywords","use","mask","license","binpkg_excludes",
            "arch","variants"], variant)

    devel = devel or merged_genpack_json.get("devel", False)

    apply_portage_sets_and_flags(variant.lower_image, 
                                merged_genpack_json.get("packages", []),
                                merged_genpack_json.get("buildtime_packages", []),
                                merged_genpack_json.get("devel_packages", []) if devel else None,
                                merged_genpack_json.get("accept_keywords", {}),
                                merged_genpack_json.get("use", {}), 
                                merged_genpack_json.get("license", {}), 
                                merged_genpack_json.get("mask", []))

    # binpkg_exclude
    binpkg_exclude = merged_genpack_json.get("binpkg_exclude", [])
    if isinstance(binpkg_exclude, str):
        binpkg_exclude = [binpkg_exclude]
    elif not isinstance(binpkg_exclude, list):
        raise ValueError("binpkg-exclude must be a string or a list of strings")

    # circular dependency breaker
    if "circulardep-breaker" in genpack_json:
        raise ValueError("Use circulardep_breaker instead of circulardep-breaker in genpack.json")
    if "circulardep_breaker" in genpack_json:
        circulardep_breaker_packages = genpack_json["circulardep_breaker"].get("packages", [])
        circulardep_breaker_use = genpack_json["circulardep_breaker"].get("use", None)
        if len(circulardep_breaker_packages) > 0:
            logging.info("Emerging circular dependency breaker packages...")
            env = {"USE": circulardep_breaker_use} if circulardep_breaker_use is not None else None
            emerge_cmd = ["emerge", "-bk", "--binpkg-respect-use=y", "-u", "--keep-going"]
            if len(binpkg_exclude) > 0:
                emerge_cmd += ["--usepkg-exclude", " ".join(binpkg_exclude)]
                emerge_cmd += ["--buildpkg-exclude", " ".join(binpkg_exclude)]
            emerge_cmd += circulardep_breaker_packages
            lower_exec(variant.lower_image, emerge_cmd, env)

    logging.info("Emerging all packages...")
    emerge_cmd = ["emerge", "-bk", "--binpkg-respect-use=y", "-uDN", "--keep-going"]
    if len(binpkg_exclude) > 0:
        emerge_cmd += ["--usepkg-exclude", " ".join(binpkg_exclude)]
        emerge_cmd += ["--buildpkg-exclude", " ".join(binpkg_exclude)]
    emerge_cmd += ["@world", "genpack-progs", "@genpack-runtime", "@genpack-buildtime"]
    if devel:
        emerge_cmd += ["@genpack-devel"]
    lower_exec(variant.lower_image, emerge_cmd)

    logging.info("Rebuilding preserved packages...")
    emerge_cmd = ["emerge", "-bk", "--binpkg-respect-use=y"]
    if len(binpkg_exclude) > 0:
        emerge_cmd += ["--usepkg-exclude", " ".join(binpkg_exclude)]
        emerge_cmd += ["--buildpkg-exclude", " ".join(binpkg_exclude)]
    emerge_cmd += ["@preserved-rebuild"]
    lower_exec(variant.lower_image, emerge_cmd)

    logging.info("Unmerging masked packages...")
    lower_exec(variant.lower_image, ["unmerge-masked-packages"])

    logging.info("Cleaning up...")
    cleanup_cmd = "emerge --depclean"
    if deep_depclean:
        cleanup_cmd += " --with-bdeps=n"
    cleanup_cmd += " && etc-update --automode -5"
    cleanup_cmd += " && eclean-dist -d"
    cleanup_cmd += " && eclean-pkg"
    if independent_binpkgs:
        cleanup_cmd += " -d" # with independent binpkgs, we can clean up binpkgs more aggressively
    lower_exec(variant.lower_image, ["sh", "-c", cleanup_cmd])

    files = []
    lib64_exists = None
    with TempMount(variant.lower_image) as mount_point:
        # check if lib64 exists
        lib64_exists = os.path.exists(os.path.join(mount_point, "lib64"))
        list_pkg_files = subprocess.Popen(
            sudo(["chroot", mount_point, "list-pkg-files"]), 
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1)
        try:
            for line in list_pkg_files.stdout:
                line = line.rstrip('\n')
                if not line or line.startswith('#'): continue
                #else
                if not os.path.isabs(line):
                    raise ValueError(f"list-pkg-files returned non-absolute path: {line}")
                #else
                files.append(line.lstrip('/'))  # remove leading slash
        finally:
            return_code = list_pkg_files.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, list_pkg_files.args)

    with open(variant.lower_files, "w") as f:
        for file in ["bin", "sbin", "lib", "usr/sbin", "run", "proc", "sys", "root", "home", "tmp", "mnt",
                     "dev", "dev/console", "dev/null"]:
            files.append(file)
        if lib64_exists:
            files.append("lib64")
        for file in sorted(files):
            f.write(file + '\n')

def bash(variant):
    logging.info("Running bash in the lower image for debugging.")
    lower_exec(variant.lower_image, "bash")

def copy_upper_files(upper_dir):
    if not os.path.isdir(upper_dir):
        raise FileNotFoundError(f"Upper directory {upper_dir} does not exist.")
    # copy mixin files
    for mixin_id in mixins:
        mixin_dir = os.path.join(mixin_root, mixin_id)
        mixin_files = os.path.join(mixin_dir, "files")
        if not os.path.isdir(mixin_files): continue
        #else
        logging.info(f"Copying files from mix-in {mixin_id} to upper directory.")
        subprocess.run(sudo(['cp', '-rdv', mixin_files + '/.', upper_dir]), check=True)

    # copy files
    if os.path.isdir("files"):
        logging.info("Copying files from 'files' directory to upper directory.")
        subprocess.run(sudo(['cp', '-rdv', 'files/.', upper_dir]), check=True)
    else:
        logging.info("No 'files' directory found, skipping file copy.")

def upper(variant):
    logging.info("Processing upper layer...")
    if not os.path.isfile(variant.lower_image) or not os.path.exists(variant.lower_files):
        raise FileNotFoundError(f"Lower image {variant.lower_image} or lower files {variant.lower_files} does not exist. Please run 'genpack lower' first.")

    subprocess.run(sudo(['mkdir', '-p', variant.upper_dir]), check=True)

    # reset upper dir by deleting files not listed in lower_files
    logging.info("Deleting upper files not listed in lower files...")
    files_to_preserve = set()
    with open(variant.lower_files, "r") as f:
        for line in f:
            line = line.rstrip('\n')
            if not line or line.startswith('#'): continue
            #else
            files_to_preserve.add(line)
            dirname = os.path.dirname(line)
            while dirname != "":
                files_to_preserve.add(dirname)
                dirname = os.path.dirname(dirname)

    files_to_remove = set()
    find = subprocess.Popen(sudo(["find", variant.upper_dir, "-printf", "%P\\n"]), stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        for line in find.stdout:
            line = line.rstrip('\n')
            if line == "": continue
            #else
            files_to_remove.add(line)
    finally:
        find.wait()
    
    for file in files_to_preserve:
        if file in files_to_remove:
            files_to_remove.remove(file)

    # safety check
    for file in files_to_remove:
        normalized = os.path.normpath(file)
        if normalized.startswith("..") or normalized.startswith("/"):
            raise ValueError(f"File to remove {file} is suspicious.")
        
    def chunk_generator(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i : i + n]

    for chunk in chunk_generator(list(files_to_remove), 64):
        subprocess.run(sudo(["rm", "-rf"] + chunk), check=True, cwd=variant.upper_dir)

    # copy-up from lower to upper
    logging.info("Copying files from lower image to upper directory...")
    with TempMount(variant.lower_image) as mount_point:
        subprocess.run(sudo(["rsync", "-a", f"--files-from={variant.lower_files}", "--relative", mount_point + "/", variant.upper_dir]), check=True)

    upper_exec(variant, ["exec-package-scripts-and-generate-metadata"])

    # merge genpack.json
    merged_genpack_json = {}
    for mixin_id in mixins:
        mixin_genpack_json = mixin_genpack_json.get(mixin_id, {})
        merge_genpack_json(merged_genpack_json, mixin_genpack_json, [f"mixin({mixin_id})", "genpack.json"], [
            "users","groups", "services", "arch"
        ])
    merge_genpack_json(merged_genpack_json, genpack_json, ["genpack.json"], [
        "users","groups", "services", "arch"
    ])

    # create groups
    groups = merged_genpack_json.get("groups", [])
    for group in groups:
        name = group if isinstance(group, str) else None
        gid = None
        if name is None:
            if not isinstance(group, dict): raise Exception("group must be string or dict")
            #else
            if "name" not in group: raise Exception("group dict must have 'name' key")
            #else
            name = group["name"]
            if "gid" in group: gid = group["gid"]
        groupadd_cmd = ["groupadd"]
        if gid is not None: groupadd_cmd += ["-g", str(gid)]
        groupadd_cmd.append(name)
        logging.info("Creating group %s..." % name)
        upper_exec(variant, groupadd_cmd)

    # create users
    users = merged_genpack_json.get("users", [])
    for user in users:
        name = user if isinstance(user, str) else None
        if name is None:
            if not isinstance(user, dict): raise Exception("user must be string or dict")
            #else
            if "name" not in user: raise Exception("user dict must have 'name' key")
            #else
            name = user["name"]
        uid = user.get("uid", None)
        comment = user.get("comment", None)
        home = user.get("home", None)
        create_home = user.get("create_home", user.get("create-home", True))
        shell = user.get("shell", None)
        initial_group = user.get("initial_group", user.get("initial-group", None))
        additional_groups = user.get("additional_groups", user.get("additional-groups", []))
        if isinstance(additional_groups, str):
            additional_groups = [additional_groups]
        elif not isinstance(additional_groups, list):
            raise Exception("additional-groups must be list or string")
        if "shell" in user: shell = user["shell"]
        empty_password = user.get("empty_password", user.get("empty-password", False))
        useradd_cmd = ["useradd"]
        if uid is not None: useradd_cmd += ["-u", str(uid)]
        if comment is not None: useradd_cmd += ["-c", comment]
        if home is not None: useradd_cmd += ["-d", home]
        if initial_group is not None: useradd_cmd += ["-g", initial_group]
        if len(additional_groups) > 0:
            useradd_cmd += ["-G", ",".join(additional_groups)]
        if shell is not None: useradd_cmd += ["-s", shell]
        if create_home: useradd_cmd += ["-m"]
        if empty_password: useradd_cmd += ["-p", ""]
        useradd_cmd.append(name)
        logging.info("Creating user %s..." % name)
        upper_exec(variant, useradd_cmd)

    copy_upper_files(variant.upper_dir)

    # execute build sctript if exists
    build_script = os.path.join(variant.upper_dir, "build")
    if os.path.isfile(build_script):
        logging.info(f"Executing build script: /build")
        upper_exec(variant, ["/build"])
    build_script_d = os.path.join(variant.upper_dir, "build.d")
    if os.path.isdir(build_script_d):
        # os.listdir returns filenames in arbitrary order, usually ASCII order on most filesystems,
        # but it is not guaranteed by Python. If you want ASCII order, sort explicitly:
        user_subdirs = []
        def determine_interpreter(script):
            if os.access(script_path, os.X_OK): return None
            #else
            if script.endswith(".sh"): return "/bin/sh"
            #else
            if script.endswith(".py"): return "/usr/bin/python"
            raise ValueError(f"Script is not executable: {script}")

        for script in sorted(os.listdir(build_script_d)):
            script_path = os.path.join(build_script_d, script)
            if os.path.isfile(script_path):
                interpreter = determine_interpreter(script_path)
                logging.info(f"Executing build script: /build.d/{script}")
                script_to_run_in_container = os.path.join("/build.d", script)
                upper_exec(variant, [script_to_run_in_container] if interpreter is None else [interpreter, script_to_run_in_container])
            elif os.path.isdir(script_path):
                user_subdirs.append(script)
                logging.info(f"Found user subdirectory in build.d: {script_path}")
        
        for subdir in user_subdirs:
            subdir_path = os.path.join(build_script_d, subdir)
            for script in sorted(os.listdir(subdir_path)):
                script_path = os.path.join(subdir_path, script)
                if not os.path.isfile(script_path):
                    logging.warning(f"Skipping non-file in /build.d/{subdir}: {script}")
                    continue
                #else
                logging.info(f"Executing build script in user subdirectory: /build.d/{subdir}/{script} as user {subdir}")
                interpreter = determine_interpreter(script_path)
                script_to_run_in_container = os.path.join("/build.d", subdir, script)
                upper_exec(variant, [script_to_run_in_container] if interpreter is None else [interpreter, script_to_run_in_container], user=subdir)

    # enable services
    services = merged_genpack_json.get("services", [])
    if len(services) > 0:
        upper_exec(variant, ["systemctl", "enable"] + services)

def upper_bash(variant):
    if not os.path.isdir(variant.upper_dir):
        raise FileNotFoundError(f"Upper directory {variant.upper_dir} does not exist. Please run 'upper' first")
    copy_upper_files(variant.upper_dir)
    logging.info("Running bash in the upper directory for debugging.")
    upper_exec(variant, ["bash"])

def pack(variant, compression=None):
    if not os.path.isfile(variant.lower_files):
        raise FileNotFoundError(f"Lower files {variant.lower_files} does not exist. Please run 'lower' first.")
    if not os.path.isdir(variant.upper_dir):
        raise FileNotFoundError(f"Upper directory {variant.upper_dir} does not exist. Please run 'upper' first")
    #else

    merged_genpack_json = {}
    merge_genpack_json(merged_genpack_json, genpack_json, ["genpack.json"], ["outfile","variants"])

    name = variant.name or genpack_json["name"]
    outfile = merged_genpack_json.get("outfile", f"{name}-{arch}.squashfs")

    if compression is None:
        compression = genpack_json.get("compression", "gzip")
    
    compression_opts = []
    if compression == "xz":
        compression_opts = ["-comp", "xz", "-b", "1M"]
    elif compression == "gzip":
        compression_opts = ["-Xcompression-level", "1"]
    elif compression == "lzo":
        compression_opts = ["-comp", "lzo"]
    elif compression == "none":
        compression_opts = ["-no-compression"]
    else:
        raise ValueError(f"Unknown compression type: {compression}")

    cmdline = ["mksquashfs", variant.upper_dir, outfile, "-wildcards", "-noappend", "-no-exports"]
    cmdline += compression_opts
    cmdline += ["-e", "build", "build.d", "build.d/*", "var/log/*.log", "var/tmp/*"]

    logging.info(f"Creating SquashFS image: {outfile} with compression {compression}")
    if os.path.exists(outfile):
        logging.info(f"Output file {outfile} already exists, removing it.")
        os.remove(outfile)
    
    subprocess.run(sudo(cmdline), check=True)
    subprocess.run(sudo(['chown', f"{os.getuid()}:{os.getgid()}", outfile]), check=True)

def get_latest_mtime(*args):
    latest = 0.0
    for arg in args:
        if isinstance(arg, float): latest = max(latest, arg)
        elif isinstance(arg, str):
            if os.path.isfile(arg):
                latest = max(latest, os.path.getmtime(arg))
            elif os.path.isdir(arg):
                latest = max(latest, os.path.getmtime(arg), get_latest_mtime(*(os.path.join(arg, f) for f in os.listdir(arg) if os.path.exists(os.path.join(arg, f)))))

        elif isinstance(arg, list):
            latest = max(latest, get_latest_mtime(*arg))

    logging.debug(f"Latest mtime from {args} is {latest}")
    return latest

def create_archive():
    logging.info("Creating archive of the current directory...")
    name = genpack_json.get("name", os.path.basename(os.getcwd()))
    archive_name = f"genpack-{name}.tar.gz"
    if os.path.isfile(archive_name):
        logging.info(f"Archive {archive_name} already exists, removing it.")
        os.remove(archive_name)

    targets = []
    if os.path.isfile("genpack.json5"): targets.append("genpack.json5")
    elif os.path.isfile("genpack.json"): targets.append("genpack.json")

    if os.path.isdir("files"): targets.append("files")
    if os.path.isdir("savedconfig"): targets.append("savedconfig")
    if os.path.isdir("patches"): targets.append("patches")
    if os.path.isdir("kernel"): targets.append("kernel")

    subprocess.run(["tar", "zcvf", archive_name] + targets, check=True)

    logging.info(f"Archive created: {archive_name}")
    return archive_name

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Genpack image Builder")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--overlay-override", default=None, help="Directory to override genpack-overlay")
    parser.add_argument("--independent-binpkgs", action="store_true", help="Use independent binpkgs, do not use shared one")
    parser.add_argument("--deep-depclean", action="store_true", help="Perform deep depclean, removing all non-runtime packages"  )
    parser.add_argument("--compression", choices=["gzip", "xz", "lzo", "none"], default=None, help="Compression type for the final SquashFS image")
    parser.add_argument("--devel", action="store_true", help="Generate development image, if supported by genpack.json")
    parser.add_argument("--variant", default=None, help="Variant to use from genpack.json, if supported")
    parser.add_argument("action", choices=["build", "lower", "bash", "upper", "upper-bash", "upper-clean", "pack", "archive"], nargs="?", default="build", help="Action to perform")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    genpack_json, genpack_json_time = load_genpack_json()
    if "name" not in genpack_json:
        genpack_json["name"] = os.path.basename(os.getcwd())
        logging.warning(f"'name' not found in genpack.json. using default: {genpack_json['name']}")  

    if not os.path.isfile(".gitignore"):
        with open(".gitignore", "w") as f:
            f.write("/work/\n")
            f.write("/*.squashfs\n")
            f.write("/*.img\n")
            f.write("/*.iso\n")
            f.write("/*.tar.gz\n")
            f.write("/.vscode/\n")
        logging.info("Created .gitignore file with default settings.")
    
    if not os.path.isdir(".vscode"):
        os.mkdir(".vscode")
        if not os.path.isfile(".vscode/settings.json"):
            with open(".vscode/settings.json", "w") as f:
                f.write('{\n')
                f.write('  "files.exclude": {"work/": true, "*.squashfs": true}\n')
                f.write('  "search.exclude": {"work/": true, "*.squashfs": true}\n')
                f.write('  "python.analysis.exclude": ["work/"]\n')
                f.write('}\n')
            logging.info("Created .vscode/settings.json with default settings.")

    if args.action == "archive":
        create_archive()
        exit(0)

    overlay_override = args.overlay_override

    independent_binpkgs = args.independent_binpkgs or genpack_json.get("independent_binpkgs", False)
    deep_depclean = args.deep_depclean

    variant = Variant(args.variant or genpack_json.get("default_variant", None))
    if variant.name is not None:
        available_variants = genpack_json.get("variants", {})
        if variant.name not in available_variants:
            raise ValueError(f"Variant '{variant.name}' is not available in genpack.json. Available variants: {list(available_variants.keys())}")

    download_mixins()

    if args.action == "bash":
        bash(variant)
        exit(0)
    elif args.action == "upper-bash":
        upper_bash(variant)
        exit(0)
    elif args.action == "upper-clean":
        raise ValueError("upper-clean is not implemented yet, use 'upper' and then remove upper directory manually.")
    #else

    if args.action in ["build", "lower"]:
        if os.path.exists(variant.lower_files):
            latest_mtime = get_latest_mtime(genpack_json_time, "savedconfig", "patches", "kernel", mixin_root)
            if os.path.getmtime(variant.lower_files) < latest_mtime:
                logging.info(f"Lower files {variant.lower_files} is outdated, rebuilding lower layer.")
                os.remove(variant.lower_files)
            elif args.action == "lower":
                os.remove(variant.lower_files)
        lower(variant, args.devel)
    if args.action in ["build", "upper"]:
        upper(variant)
    if args.action in ["build", "pack"]:
        pack(variant, args.compression)
