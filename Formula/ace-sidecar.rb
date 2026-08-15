class AceSidecar < Formula
  include Language::Python::Virtualenv

  desc "Local developer observability sidecar and skill miner for Claude Code & Antigravity"
  homepage "https://github.com/ACE-Engineering/ace-sidecar"
  url "https://files.pythonhosted.org/packages/source/a/ace-sidecar/ace-sidecar-0.1.1.tar.gz"
  # Fill in once the sdist is on PyPI — the digest cannot be known before then:
  #   shasum -a 256 dist/ace_sidecar-0.1.1.tar.gz
  # Left as a placeholder deliberately: it fails `brew install` on a checksum
  # mismatch, which is the right outcome while the release is not yet published.
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "AGPL-3.0-or-later"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  test do
    system "#{bin}/ace", "--help"
  end
end
