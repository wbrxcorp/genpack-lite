import sys,json5

sys.path.insert(0,"src")
import genpack

genpack_json = json5.loads("""
{
    name: "test-package",
    outfile: "test-package-1.0.0.tbz2",
    packages: ["test-package-1.0.0"],
    buildtime_packages: ["test-buildtime-package"],
    devel_packages: ["test-devel-package"],
    accept_keywords: {
        "common-package": null,
    },
    arch: {
        "x86_64": {
            accept_keywords: {
                "package-for-x86_64": null,
            },
            variants: {
                "variant-must-be-ignored": {
                    packages: ["this-package-must-not-be-listed"],
                }
            }
        },
        "aarch64": {
            accept_keywords: {
                "package-for-aarch64": null,
            },
        }
    },
    variants: {
        "test-variant": {
            "packages": ["package-for-variant"],
            "accept_keywords": {
                "package-for-variant": null,
            },
        },
        "another-variant": {
            "accept_keywords": {
                "package-for-another-variant": null,
            },
        }
    }
}
""")

genpack.arch = "x86_64"
genpack.variant = "test-variant"

merged_genpack_json = {}

genpack.merge_genpack_json(
    merged_genpack_json,
    genpack_json,
    ["genpack.json"],
    allowed_properties=["name", "outfile", "packages", "buildtime_packages", "devel_packages",
                        "accept_keywords", "use", "mask", "license", "binpkg_exclude", "users", "groups",
                        "arch", "variants"]
)

print("\nMerged genpack.json:")
print(merged_genpack_json)
