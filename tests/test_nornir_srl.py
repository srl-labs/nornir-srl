import json
import os
import tempfile
from nornir_srl import __version__
from nornir_srl.mcp_server import list_topologies


def test_version():
    assert __version__ == "0.4.1"


def test_list_topologies_recursive():
    """Test that list_topologies finds files recursively and deduplicates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a nested structure
        # dir/a.clab.yml
        # dir/sub/b.clab.yml
        # dir/sub/nornir_config.yaml
        # dir/nornir_config.yaml

        os.makedirs(os.path.join(tmpdir, "sub"))

        files = [
            "a.clab.yml",
            "c.clab.yaml",
            "clab-test.yml",
            "sub/b.clab.yml",
            "sub/clab-nested.yml",
            "nornir_config.yaml",
            "sub/nornir_config.yaml",
        ]

        for f in files:
            with open(os.path.join(tmpdir, f), "w") as fp:
                fp.write("{}")

        # Run list_topologies
        result_json = list_topologies(tmpdir)
        result = json.loads(result_json)

        # Verify containerlab topologies
        clab_topos = result["containerlab_topologies"]
        assert len(clab_topos) == 5
        # Check some files are there (as absolute paths)
        assert any(f.endswith("a.clab.yml") for f in clab_topos)
        assert any(f.endswith("c.clab.yaml") for f in clab_topos)
        assert any(f.endswith("sub/b.clab.yml") for f in clab_topos)
        assert any(f.endswith("clab-test.yml") for f in clab_topos)
        assert any(f.endswith("sub/clab-nested.yml") for f in clab_topos)

        # Verify nornir configs
        nornir_configs = result["nornir_configs"]
        assert len(nornir_configs) == 2
        assert any(f.endswith("nornir_config.yaml") for f in nornir_configs)
        assert any(f.endswith("sub/nornir_config.yaml") for f in nornir_configs)


def test_list_topologies_deduplication():
    """Test that list_topologies deduplicates files matching multiple patterns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # file matches both *.clab.yml and clab-*.yml
        filename = "clab-test.clab.yml"
        with open(os.path.join(tmpdir, filename), "w") as fp:
            fp.write("{}")

        result_json = list_topologies(tmpdir)
        result = json.loads(result_json)

        clab_topos = result["containerlab_topologies"]
        assert len(clab_topos) == 1
        assert clab_topos[0].endswith(filename)
