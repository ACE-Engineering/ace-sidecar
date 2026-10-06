class AceSidecar < Formula
  include Language::Python::Virtualenv

  desc "Local developer observability sidecar and skill miner for Claude Code & Antigravity"
  homepage "https://github.com/ACE-Engineering/ace-sidecar"
  # The filename carries an underscore even though the project name is hyphenated:
  # PEP 625 has build backends normalise it, so .../ace-sidecar-0.1.1.tar.gz is a 404.
  url "https://files.pythonhosted.org/packages/source/a/ace-sidecar/ace_sidecar-0.2.0.tar.gz"
  sha256 "785a7280627bbe3b7b583733dbc95d426ff02ea50d18d5312555129da6b572cc"
  license "AGPL-3.0-or-later"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  test do
    system "#{bin}/ace", "--help"
  end
end
