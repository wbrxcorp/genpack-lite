#!/usr/bin/python3
# -*- coding: utf-8 -*-
import os,logging,tempfile,subprocess,re,json,argparse
from datetime import datetime
import requests

arch = os.uname().machine
work_root = "work"
work_dir = os.path.join(work_root, arch)
lower_image = os.path.join(work_dir, "lower.img")
upper_dir = os.path.join(work_dir, "upper")
base_url = "http://ftp.iij.ad.jp/pub/linux/gentoo/"
user_agent = "genpack/0.1"
overlay_source = "https://github.com/wbrxcorp/genpack-overlay.git"
genpack_json = None

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

def get_latest_stage3_tarball_url(variant = "systemd"):
    _arch = arch
    _arch2 = arch
    if _arch == "x86_64": _arch = _arch2 = "amd64"
    elif _arch == "i686": _arch = "x86"
    elif _arch == "aarch64": _arch = _arch2 = "arm64"
    elif _arch == "riscv64":
        _arch = "riscv"
        _arch2 = "rv64_lp64d"
    current_status = None
    for line in url_readlines(base_url + "releases/" + _arch + "/autobuilds/latest-stage3-" + _arch2 + "-%s.txt" % (variant,)):
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
    raise Exception("No stage3 tarball (arch=%s,variant=%s) found", arch, variant)

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

def create_sparse_file(file_path, size_in_mib):
    """Create a sparse file of the specified size."""
    logging.info(f"Creating sparse file at {file_path} with size {size_in_mib} MiB")
    with open(file_path, 'wb') as f:
        f.seek(size_in_mib * 1024 * 1024 - 1)
        f.write(b'\0')
    logging.info(f"Sparse file created: {file_path}")

def format_filesystem(file_path):
    logging.info(f"Formatting filesystem on {file_path}")
    subprocess.run(['mkfs.xfs', file_path], check=True)

def setup_lower_image(lower_image, stage3_tarball, portage_tarball):
    create_sparse_file(lower_image, 16384)  # Create a 16 GiB sparse file
    try:
        format_filesystem(lower_image)
        logging.info(f"Lower image created and formatted: {lower_image}")

        with TempMount(lower_image) as mount_point:
            logging.info(f"Using temporary mount point: {mount_point}")
            # Here you would perform operations on the mounted filesystem
            # For example, copying files, etc.
            logging.info("Performing operations on the mounted filesystem...")
            subprocess.run(sudo(['tar', 'xvpf', stage3_tarball, '-C', mount_point]), check=True)
            portage_dir = os.path.join(mount_point, "var/db/repos/gentoo")
            subprocess.run(sudo(["mkdir", "-p", portage_dir]), check=True)
            subprocess.run(sudo(['tar', 'xvpf', portage_tarball, '-C', portage_dir, "--strip-components=1"]), check=True)
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
        subprocess.run(sudo(['tar', 'xvpf', portage_tarball, '-C', portage_dir, "--strip-components=1"]), check=True)
        logging.info("Portage replaced successfully.")

def lower_exec(lower_image, cmdline):
    if isinstance(cmdline, str):
        cmdline = [cmdline]
    # use PID for container name
    container_name = "genpack-%d" % os.getpid()
    cache_dir = os.path.join(work_dir, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    nspawn_cmdline = ["systemd-nspawn", "-q", "--suppress-sync=true", 
        "-M", container_name, f"--image={lower_image}",
        "--bind=%s:/var/cache" % os.path.abspath(cache_dir),
        "--capability=CAP_MKNOD,CAP_SYS_ADMIN",
    ]
    nspawn_cmdline += cmdline

    subprocess.run(sudo(nspawn_cmdline), check=True)

def escape_colon(s):
    # systemd-nspawn's some options need colon to be escaped
    return re.sub(r':', r'\:', s)

def upper_exec(lower_image, upper_dir, cmdline):
    # convert command to list if it is string
    if isinstance(cmdline, str): cmdline = [cmdline]
    container_name = "genpack-%d" % os.getpid()
    cache_dir = os.path.join(work_dir, "cache")
    subprocess.check_call(sudo(["systemd-nspawn", "-q", "--suppress-sync=true", "-M", container_name, 
        f"--image={lower_image}", "--overlay=+/:%s:/" % escape_colon(os.path.abspath(upper_dir)),
        "--bind=%s:/var/cache" % os.path.abspath(cache_dir),
        "--capability=CAP_MKNOD"]
        + cmdline))

def sync_genpack_overlay(lower_image):
    with TempMount(lower_image) as mount_point:
        genpack_overlay_dir = os.path.join(mount_point, "var/db/repos/genpack-overlay")
        if os.path.isdir(genpack_overlay_dir):
            subprocess.run(sudo(['git', '-C', genpack_overlay_dir, 'pull']), check=True)
        else:
            logging.info("Genpack overlay not found, cloning...")
            subprocess.run(sudo(['git', 'clone', overlay_source, genpack_overlay_dir]), check=True)
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

def apply_portage_flags(lower_image, accept_keywords, use, license, mask):
    with TempMount(lower_image) as mount_point:
        if accept_keywords is None: accept_keywords = {}
        if use is None: use = {}
        if license is None: license = {}

        accept_keywords_file = os.path.join(mount_point, "etc/portage/package.accept_keywords/genpack")
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

        use_file = os.path.join(mount_point, "etc/portage/package.use/genpack")
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
        
        license_file = os.path.join(mount_point, "etc/portage/package.license/genpack")
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
        
        mask_file = os.path.join(mount_point, "etc/portage/package.mask/genpack")
        logging.info(f"Applying masked packages to {mask_file}")
        subprocess.run(sudo(['mkdir', '-p', os.path.dirname(mask_file)]), check=True)
        tee = subprocess.Popen(sudo(['tee', mask_file]), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        try:
            for pkg in mask:
                tee.stdin.write(f"{pkg}\n")
        finally:
            tee.stdin.close()
            tee.wait()

def set_gentoo_profile(lower_image, profile_name):
    with TempMount(lower_image) as mount_point:
        portage_dir = os.path.join(mount_point, "var/db/repos/gentoo")
        profiles_default_linux_dir = os.path.join(portage_dir, "profiles/default/linux")
        if not os.path.isdir(profiles_default_linux_dir):
            raise Exception(f"Portage directory {portage_dir} does not contain profiles/default/linux directory.")
        arch_map = {
            "loongarch64": "loong",
            "ppc": "powerpc",
            "riscv64": "riscv",
            "riscv32": "riscv",
            "i686": "x86",
            "x86_64": "amd64",
            "aarch64": "arm64",
        }
        portage_arch = arch_map.get(arch, arch)
        arch_dir = os.path.join(profiles_default_linux_dir, portage_arch)
        if not os.path.isdir(arch_dir):
            raise Exception(f"Portage directory {portage_dir} does not contain profiles/default/linux/{portage_arch} directory.")
        # enum subdirectories in arch_dir
        subdirs = [float(d) for d in os.listdir(arch_dir) if os.path.isdir(os.path.join(arch_dir, d))]
        if len(subdirs) == 0:
            raise Exception(f"Portage directory {portage_dir} does not contain any subdirectories in profiles/default/linux/{portage_arch} directory.")
        #else
        #pick the latest subdirectory
        latest_subdir = max(subdirs)
        latest_profile_dir = os.path.join(arch_dir, str(latest_subdir))
        exact_profile_dir = os.path.join(latest_profile_dir, profile_name)
        if not os.path.isdir(exact_profile_dir):
            raise Exception(f"Portage directory {portage_dir} does not contain profiles/default/linux/{portage_arch}/{latest_subdir}/{profile_name} directory.")
        #else
        exact_profile = os.path.join(f"default/linux/{portage_arch}/{latest_subdir}", profile_name)
        logging.info(f"Setting Gentoo profile to {exact_profile} in {mount_point}")
        subprocess.run(sudo(['chroot', mount_point, "eselect", "profile", "set", exact_profile]), check=True)
        logging.info(f"Gentoo profile set to {exact_profile} successfully.")

def lower(bash=False):
    logging.info("Starting genpack setup...")
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

    if stage3_is_new or not os.path.isfile(lower_image):
        setup_lower_image(lower_image, stage3_tarball, portage_tarball)
        with open(stage3_saved_headers_path, 'w') as f:
            f.write(headers_to_info(stage3_headers))
    elif portage_is_new:
        replace_portage(lower_image, portage_tarball)

    if portage_is_new:
        with open(portage_saved_headers_path, 'w') as f:
            f.write(headers_to_info(portage_headers))
    
    gentoo_profile = genpack_json.get("gentoo-profile", None)
    if gentoo_profile is not None and stage3_is_new:
        set_gentoo_profile(lower_image, gentoo_profile)
    
    sync_genpack_overlay(lower_image)

    accept_keywords = {
        "dev-cpp/argparse":None # argparse is required for genpack-progs
    } | genpack_json.get("accept_keywords", {})
    use = {
        "sys-libs/glibc": "audit", # Intentionally causing glibc to be rebuilt
        "sys-kernel/installkernel":"dracut", # genpack depends on dracut
        "dev-lang/perl":"minimal",
        "app-editors/vim":"minimal"
    } | genpack_json.get("use", {})
    license = genpack_json.get("license", {})
    mask = genpack_json.get("mask", [])
    apply_portage_flags(lower_image, accept_keywords, use, license, mask)

    if bash:
        logging.info("Running bash in the lower image for debugging.")
        lower_exec(lower_image, "bash")
        return

    #else
    lower_exec(lower_image, ["emerge", "-bk", "--binpkg-respect-use=y", "-uDN", "genpack-progs", "--keep-going"])

    lower_exec(lower_image, ["emaint", "binhost", "--fix"])
    packages = genpack_json.get("packages", [])
    if len(packages) > 0:
        lower_exec(lower_image, ["emerge", "-bk", "--binpkg-respect-use=y", "-uDN", "--keep-going", "world"] + packages)

    #lower_exec(lower_image, "bash")

    lower_exec(lower_image, ["emerge", "-bk", "--binpkg-respect-use=y", "@preserved-rebuild"])
    lower_exec(lower_image, ["emerge", "--depclean"])
    lower_exec(lower_image, ["etc-update", "--automode", "-5"])
    lower_exec(lower_image, ["eclean-dist", "-d"])
    lower_exec(lower_image, ["eclean-pkg", "-d"])

def upper(bash=False):
    packages = genpack_json.get("packages", [])
    if len(packages) == 0:
        logging.info("No packages specified in genpack.json.")
        return
    #else
    if os.path.exists(upper_dir):
        logging.info(f"Upper directory {upper_dir} already exists, removing.")
        subprocess.run(sudo(['rm', '-rf', upper_dir]), check=True)
    subprocess.run(sudo(['mkdir', '-p', upper_dir]), check=True)
    logging.info(f"Upper directory created: {upper_dir}")

    logging.info("Executing copyup-packages...")
    cmdline = ["/usr/bin/copyup-packages", "--bind-mount-root", "--toplevel-dirs", "--exec-package-scripts"]
    cmdline += ["--generate-metadata"]
    cmdline += packages

    upper_exec(lower_image, upper_dir, cmdline)

    # create groups
    groups = genpack_json.get("groups", [])
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
        upper_exec(lower_image, upper_dir, groupadd_cmd)

    # create users
    users = genpack_json.get("users", [])
    for user in users:
        name = user if isinstance(user, str) else None
        uid = None
        comment = None
        home = None
        create_home = True
        initial_group = None
        additional_groups = []
        shell = None
        if name is None:
            if not isinstance(user, dict): raise Exception("user must be string or dict")
            #else
            if "name" not in user: raise Exception("user dict must have 'name' key")
            #else
            name = user["name"]
        if "uid" in user: uid = user["uid"]
        if "comment" in user: comment = user["comment"]
        if "home" in user: home = user["home"]
        if "initial_group" in user: initial_group = user["initial_group"]
        if "additional_groups" in user:
            if not isinstance(user["additional_groups"], list): raise Exception("additional_groups must be list")
            #else
            additional_groups = user["additional_groups"]
        if "shell" in user: shell = user["shell"]
        useradd_cmd = ["useradd"]
        if uid is not None: useradd_cmd += ["-u", str(uid)]
        if comment is not None: useradd_cmd += ["-c", comment]
        if home is not None: useradd_cmd += ["-d", home]
        if initial_group is not None: useradd_cmd += ["-g", initial_group]
        if len(additional_groups) > 0:
            useradd_cmd += ["-G", ",".join(additional_groups)]
        if shell is not None: useradd_cmd += ["-s", shell]
        if create_home: useradd_cmd += ["-m"]
        useradd_cmd.append(name)
        logging.info("Creating user %s..." % name)
        upper_exec(lower_image, upper_dir, useradd_cmd)

    # copy files
    if os.path.isdir("files"):
        logging.info("Copying files from 'files' directory to upper directory.")
        subprocess.run(sudo(['cp', '-rdv', 'files/.', upper_dir]), check=True)
    else:
        logging.info("No 'files' directory found, skipping file copy.")

    # execute build sctript if exists
    build_script = os.path.join(upper_dir, "build")
    if os.path.isfile(build_script):
        logging.info(f"Executing build script: /build")
        upper_exec(lower_image, upper_dir, ["/build"])
        # remove build script after execution
        subprocess.run(sudo(['rm', '-f', build_script]), check=True)
    else:
        logging.info("No build script found.")
    
    if bash:
        logging.info("Running bash in the upper directory for debugging.")
        upper_exec(lower_image, upper_dir, "bash")
        return

    # else
    # enable services
    services = genpack_json.get("services", [])
    if len(services) > 0:
        upper_exec(lower_image, upper_dir, ["systemctl", "enable"] + services)

    name = genpack_json.get("name", "genpack")
    outfile = genpack_json.get("outfile", f"{name}-{arch}.squashfs")
    compression = genpack_json.get("compression", "gzip")
    cmdline = ["mksquashfs", upper_dir, outfile, "-noappend", "-no-exports"]
    if compression == "xz": cmdline += ["-comp", "xz", "-b", "1M"]
    elif compression == "gzip": cmdline += ["-Xcompression-level", "1"]
    elif compression == "lzo": cmdline += ["-comp", "lzo"]
    elif compression == "none": cmdline += ["-no-compression"]
    else:
        raise ValueError(f"Unknown compression type: {compression}")
    subprocess.run(sudo(cmdline), check=True)
    subprocess.check_call(sudo(["chown", "%d:%d" % (os.getuid(), os.getgid()), outfile]))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Genpack image Builder")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("action", choices=["build", "lower", "bash", "upper", "upper-bash"], nargs="?", default="build", help="Action to perform")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    if not os.path.isfile("genpack.json"):
        raise FileNotFoundError("genpack.json file not found. Please create it in the current directory.")
    #else
    with open("genpack.json", "r") as f:
        genpack_json = json.load(f)

    if not os.path.isfile(".gitignore"):
        with open(".gitignore", "w") as f:
            f.write("work/\n")
            f.write("*.squashfs\n")
            f.write(".vscode/\n")
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

    if args.action in ["build", "lower", "bash"]:
        lower(args.action == "bash")
    if args.action in ["build", "upper", "upper-bash"]:
        upper(args.action == "upper-bash")
