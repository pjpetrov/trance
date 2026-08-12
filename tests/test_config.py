

# ------------------------------------------------- output token budget

def test_output_room_scales_with_the_window_not_a_flat_4096():
    """Regression: a 4096-token reply cap cut write_file calls off mid-string,
    and llama.cpp rejected the half-written JSON with a 500."""
    from trance.config import Config
    from trance.providers.base import ModelPreset, ProviderConfig

    config = Config()
    config.providers["local"] = ProviderConfig(name="local", kind="llamacpp")   # 64k
    config.presets["big"] = ModelPreset(name="big", provider="local")
    resolved = config.resolve(config.worker, preset="big")

    assert resolved.max_tokens == 8000            # a whole file fits in a reply
    assert resolved.input_budget > 50_000         # and context still dominates


def test_an_explicit_preset_setting_wins():
    from trance.config import Config
    from trance.providers.base import ModelPreset, ProviderConfig

    config = Config()
    config.providers["local"] = ProviderConfig(name="local", kind="llamacpp")
    config.presets["big"] = ModelPreset(name="big", provider="local", max_tokens=16384)
    assert config.resolve(config.worker, preset="big").max_tokens == 16384


def test_output_room_is_capped_on_a_huge_window():
    from trance.config import MAX_OUTPUT_TOKENS, default_output_tokens

    assert default_output_tokens(1_000_000, 4096) == MAX_OUTPUT_TOKENS
    assert default_output_tokens(200_000, 4096) == 25_000
    assert default_output_tokens(8_000, 4096) == 4096      # never below the floor


def test_a_top_level_key_below_a_section_header_still_lands(tmp_path):
    """TOML makes everything after a [section] header a key of that section —
    so `system_dir` appended at the bottom of the file landed inside
    [curator] and silently did nothing, while the server provisioned every
    new project from the legacy state it was supposed to leave behind."""
    from trance.config import Config

    path = tmp_path / "trance.toml"
    path.write_text(
        "[curator]\n"
        "max_hops = 2\n"
        "runs_dir = \"elsewhere\"\n"
        "system_dir = \"~/.trance-test\"\n",
        encoding="utf8")

    cfg = Config.load(path)
    assert cfg.system_dir == "~/.trance-test"
    assert cfg.runs_dir == "elsewhere"
    assert cfg.curator.max_hops == 2          # the section's own keys stay its
