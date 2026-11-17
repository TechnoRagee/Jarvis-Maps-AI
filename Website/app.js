/**
 * Jarvis Maps AI — Official Web Portal Logic
 * SPA routing, interactive GIS simulator, SmartScreen walk-through, and verification engine.
 */

// Production Release Metadata
const RELEASE_CONFIG = {
  version: "v1.1.0-LTS",
  filename: "JarvisMapsAI_Setup_v1.1.exe",
  filesize: "411 MB",
  sha256: "0763cf8f9946a29aea83b1ef5f032face301c3fd3065b6b0ae5772074835cd97",
  downloadUrl: "https://github.com/TechnoRagee/Jarvis-Maps-AI/releases/download/v1.1.0/JarvisMapsAI_Setup_v1.1.exe",
  zipFilename: "JarvisMapsAI_Windows_x64.zip",
  zipUrl: "https://github.com/TechnoRagee/Jarvis-Maps-AI/releases/download/v1.1.0/JarvisMapsAI_Setup_v1.1.exe"
};

// 1. Router System
function navigateTo(hash) {
  const cleanHash = (hash || "#home").replace("#", "") || "home";
  const views = document.querySelectorAll(".view-section");
  const navLinks = document.querySelectorAll(".nav-link");

  let targetView = document.getElementById("view-" + cleanHash);
  if (!targetView) {
    targetView = document.getElementById("view-404") || document.getElementById("view-home");
  }

  views.forEach(v => v.classList.remove("active"));
  if (targetView) {
    targetView.classList.add("active");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  navLinks.forEach(link => {
    const href = link.getAttribute("href") || "";
    if (href.replace("#", "") === cleanHash) {
      link.classList.add("active");
    } else {
      link.classList.remove("active");
    }
  });

  // Re-render canvas if entering preview or home
  if (cleanHash === "home") {
    initHeroCanvas();
  } else if (cleanHash === "preview") {
    initStudioPreview();
  }
}

window.addEventListener("hashchange", () => {
  navigateTo(window.location.hash);
});

// 2. Toast Notification Engine
function showToast(message, icon = "✓") {
  let shelf = document.getElementById("toast-shelf");
  if (!shelf) {
    shelf = document.createElement("div");
    shelf.id = "toast-shelf";
    shelf.className = "toast-shelf";
    document.body.appendChild(shelf);
  }

  const toast = document.createElement("div");
  toast.className = "toast";
  toast.innerHTML = `<span style="color: #34d399; font-weight: bold;">${icon}</span> <span>${message}</span>`;
  shelf.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(100%)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(() => toast.remove(), 300);
  }, 3200);
}

// 3. Copy to Clipboard
function copyText(text, successMsg = "Copied to clipboard!") {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => {
      showToast(successMsg);
    }).catch(() => fallbackCopy(text, successMsg));
  } else {
    fallbackCopy(text, successMsg);
  }
}

function fallbackCopy(text, successMsg) {
  const textArea = document.createElement("textarea");
  textArea.value = text;
  textArea.style.position = "fixed";
  textArea.style.left = "-9999px";
  document.body.appendChild(textArea);
  textArea.focus();
  textArea.select();
  try {
    document.execCommand('copy');
    showToast(successMsg);
  } catch (err) {
    showToast("Failed to copy", "✕");
  }
  document.body.removeChild(textArea);
}

// 4. Download Trigger
function triggerDownload() {
  showToast(`Initiating direct download of ${RELEASE_CONFIG.filename}...`, "⬇");
  const a = document.createElement("a");
  a.href = RELEASE_CONFIG.downloadUrl;
  a.download = RELEASE_CONFIG.filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// 5. Accordion FAQ
function initFAQ() {
  const triggers = document.querySelectorAll(".faq-trigger");
  triggers.forEach(trig => {
    trig.addEventListener("click", () => {
      const parent = trig.closest(".faq-item");
      const wasOpen = parent.classList.contains("open");
      
      // Close sibling FAQs in the same container for clean accordion UX
      const siblingItems = parent.parentElement.querySelectorAll(".faq-item");
      siblingItems.forEach(i => i.classList.remove("open"));

      if (!wasOpen) {
        parent.classList.add("open");
      }
    });
  });

  // Search FAQ filter
  const searchInput = document.getElementById("faq-search-input");
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      const q = e.target.value.toLowerCase().trim();
      const allFaqItems = document.querySelectorAll(".faq-item");
      allFaqItems.forEach(item => {
        const text = item.innerText.toLowerCase();
        if (!q || text.includes(q)) {
          item.style.display = "block";
        } else {
          item.style.display = "none";
        }
      });
    });
  }

  // Tag filter pills
  const tagButtons = document.querySelectorAll(".faq-tag-btn");
  tagButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      tagButtons.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const category = btn.dataset.category || "all";
      const items = document.querySelectorAll(".faq-item");
      items.forEach(item => {
        if (category === "all" || item.dataset.category === category) {
          item.style.display = "block";
        } else {
          item.style.display = "none";
        }
      });
    });
  });
}

// 6. Interactive SmartScreen Simulator
function initSmartScreenSim() {
  const moreInfoLink = document.getElementById("ss-more-info");
  const hiddenPane = document.getElementById("ss-hidden-details");
  const runAnywayBtn = document.getElementById("ss-run-anyway");
  const statusMsg = document.getElementById("ss-status-msg");

  if (moreInfoLink && hiddenPane && runAnywayBtn) {
    moreInfoLink.addEventListener("click", () => {
      hiddenPane.classList.add("active");
      runAnywayBtn.classList.add("active");
      moreInfoLink.style.display = "none";
      if (statusMsg) {
        statusMsg.innerText = "Step 2: Click 'Run anyway' below to launch JarvisMapsAI.exe.";
        statusMsg.style.color = "#34d399";
      }
    });

    runAnywayBtn.addEventListener("click", () => {
      showToast("SmartScreen bypassed! Launching JarvisMapsAI.exe...", "🚀");
      if (statusMsg) {
        statusMsg.innerText = "Simulation Success! The security gatekeeper dialog will now appear.";
      }
    });
  }
}

// 7. Passcode Simulation Dialog
function openPasscodeModal() {
  const modal = document.getElementById("passcode-modal");
  if (modal) {
    modal.classList.add("open");
    const input = document.getElementById("passcode-modal-input");
    if (input) {
      input.value = "";
      input.focus();
    }
    const feedback = document.getElementById("passcode-modal-feedback");
    if (feedback) {
      feedback.textContent = "";
      feedback.className = "passcode-feedback";
    }
  }
}

function closePasscodeModal() {
  const modal = document.getElementById("passcode-modal");
  if (modal) modal.classList.remove("open");
}

function verifyPasscodeModal() {
  const input = document.getElementById("passcode-modal-input");
  const feedback = document.getElementById("passcode-modal-feedback");
  if (!input || !feedback) return;

  const val = input.value.trim().toUpperCase();
  if (!val) {
    feedback.className = "passcode-feedback error";
    feedback.innerHTML = "✕ Please enter a valid license key or authorization key.";
    return;
  }

  if (val.startsWith("JM-") || val.length >= 10) {
    feedback.className = "passcode-feedback success";
    feedback.innerHTML = "✓ ACCESS GRANTED: License Key Verified! Spatial Engine Initialized.";
    showToast("License Key Verified! Access Granted.", "🔓");
    setTimeout(() => {
      closePasscodeModal();
    }, 1200);
  } else {
    feedback.className = "passcode-feedback error";
    feedback.innerHTML = "✕ ACCESS DENIED: Invalid license format or unauthorized key.";
  }
}

// 8. Animated Hero Map Canvas
function initHeroCanvas() {
  const canvas = document.getElementById("hero-map-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  function resize() {
    canvas.width = canvas.parentElement.clientWidth;
    canvas.height = canvas.parentElement.clientHeight;
  }
  resize();
  window.addEventListener("resize", resize);

  // Geometric Nodes representing cadastral mesh
  const nodes = [
    { x: 0.28, y: 0.35, vx: 0.0003, vy: -0.0002 },
    { x: 0.42, y: 0.22, vx: -0.0002, vy: 0.0003 },
    { x: 0.58, y: 0.26, vx: 0.0002, vy: 0.0002 },
    { x: 0.72, y: 0.48, vx: -0.0003, vy: -0.0002 },
    { x: 0.62, y: 0.75, vx: 0.0002, vy: -0.0003 },
    { x: 0.36, y: 0.68, vx: -0.0002, vy: 0.0002 }
  ];

  let frame = 0;
  function animate() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const w = canvas.width;
    const h = canvas.height;

    // Draw background grid lines
    ctx.strokeStyle = "rgba(255, 255, 255, 0.03)";
    ctx.lineWidth = 1;
    for (let x = 0; x < w; x += 50) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
    }
    for (let y = 0; y < h; y += 50) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    // Update node positions subtly
    nodes.forEach(n => {
      n.x += n.vx;
      n.y += n.vy;
      if (n.x < 0.15 || n.x > 0.85) n.vx *= -1;
      if (n.y < 0.15 || n.y > 0.85) n.vy *= -1;
    });

    // Draw polygon fill
    ctx.beginPath();
    ctx.moveTo(nodes[0].x * w, nodes[0].y * h);
    for (let i = 1; i < nodes.length; i++) {
      ctx.lineTo(nodes[i].x * w, nodes[i].y * h);
    }
    ctx.closePath();
    ctx.fillStyle = "rgba(6, 182, 212, 0.08)";
    ctx.fill();

    // Draw polygon border
    ctx.strokeStyle = "rgba(6, 182, 212, 0.65)";
    ctx.lineWidth = 1.8;
    ctx.stroke();

    // Draw nodes & coordinates
    nodes.forEach((n, idx) => {
      const nx = n.x * w;
      const ny = n.y * h;

      // Glow circle
      ctx.beginPath();
      ctx.arc(nx, ny, 7 + Math.sin(frame * 0.05 + idx) * 2, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(99, 102, 241, 0.35)";
      ctx.fill();

      // Node point
      ctx.beginPath();
      ctx.arc(nx, ny, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = "#38bdf8";
      ctx.fill();

      // Label
      ctx.fillStyle = "rgba(255, 255, 255, 0.6)";
      ctx.font = "10px JetBrains Mono";
      ctx.fillText(`N-0${idx + 1}`, nx + 10, ny - 6);
    });

    // Centroid pulse
    const cx = nodes.reduce((acc, n) => acc + n.x * w, 0) / nodes.length;
    const cy = nodes.reduce((acc, n) => acc + n.y * h, 0) / nodes.length;

    ctx.beginPath();
    ctx.arc(cx, cy, 14 + Math.sin(frame * 0.08) * 4, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(16, 185, 129, 0.5)";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    ctx.beginPath();
    ctx.arc(cx, cy, 4, 0, Math.PI * 2);
    ctx.fillStyle = "#10b981";
    ctx.fill();

    ctx.fillStyle = "#34d399";
    ctx.font = "11px JetBrains Mono";
    ctx.fillText("CENTROID [37.7749° N, 122.4194° W]", cx + 18, cy + 4);

    frame++;
    requestAnimationFrame(animate);
  }
  animate();
}

// 9. Interactive Desktop Studio Preview Inspector
function initStudioPreview() {
  const menuBtns = document.querySelectorAll(".preview-menu-btn");
  const areaVal = document.getElementById("inspector-area-val");
  const perimeterVal = document.getElementById("inspector-perim-val");
  const vertexVal = document.getElementById("inspector-vertices-val");

  menuBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      menuBtns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");

      const mod = btn.dataset.module;
      if (mod === "boundaries") {
        if (areaVal) areaVal.innerText = "42,850 m²";
        if (perimeterVal) perimeterVal.innerText = "840.2 m";
        if (vertexVal) vertexVal.innerText = "16 Pts";
      } else if (mod === "amenities") {
        if (areaVal) areaVal.innerText = "Buffer: 500m";
        if (perimeterVal) perimeterVal.innerText = "Isochrone: 15m";
        if (vertexVal) vertexVal.innerText = "238 POIs";
      } else if (mod === "coords") {
        if (areaVal) areaVal.innerText = "EPSG: 4326";
        if (perimeterVal) perimeterVal.innerText = "WGS84 UTM 32N";
        if (vertexVal) vertexVal.innerText = "12.4k pts";
      }
      showToast(`Switched active viewport module to: ${btn.innerText.trim()}`);
    });
  });
}

// 10. Initialization on DOMContentLoaded
document.addEventListener("DOMContentLoaded", () => {
  // Setup router
  navigateTo(window.location.hash);

  // Setup FAQs
  initFAQ();

  // Setup SmartScreen simulator
  initSmartScreenSim();

  // Setup initial hero canvas
  initHeroCanvas();

  // Setup preview
  initStudioPreview();

  // Mobile nav toggle
  const mobileToggle = document.getElementById("mobile-nav-toggle");
  const navLinks = document.querySelector(".nav-links");
  if (mobileToggle && navLinks) {
    mobileToggle.addEventListener("click", () => {
      navLinks.style.display = navLinks.style.display === "flex" ? "none" : "flex";
      navLinks.style.flexDirection = "column";
      navLinks.style.position = "absolute";
      navLinks.style.top = "60px";
      navLinks.style.left = "20px";
      navLinks.style.right = "20px";
      navLinks.style.background = "#0c0f1a";
      navLinks.style.padding = "20px";
      navLinks.style.borderRadius = "14px";
      navLinks.style.border = "1px solid rgba(255,255,255,0.1)";
    });
  }

  // Global Passcode Modal triggers
  const passcodeTriggers = document.querySelectorAll(".btn-open-passcode-modal");
  passcodeTriggers.forEach(btn => {
    btn.addEventListener("click", openPasscodeModal);
  });

  const modalClose = document.getElementById("modal-close");
  if (modalClose) modalClose.addEventListener("click", closePasscodeModal);

  const modalVerifyBtn = document.getElementById("modal-verify-btn");
  if (modalVerifyBtn) modalVerifyBtn.addEventListener("click", verifyPasscodeModal);

  const modalInput = document.getElementById("passcode-modal-input");
  if (modalInput) {
    modalInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") verifyPasscodeModal();
      if (e.key === "Escape") closePasscodeModal();
    });
  }

  // Checksum copy triggers
  const copyChecksumBtns = document.querySelectorAll(".btn-copy-checksum");
  copyChecksumBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      copyText(RELEASE_CONFIG.sha256, "Copied SHA-256 Checksum to clipboard!");
    });
  });

  const copyPsBtns = document.querySelectorAll(".btn-copy-powershell");
  copyPsBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      const psCmd = `Get-FileHash -Path .\\JarvisMapsAI_Windows_x64.zip -Algorithm SHA256`;
      copyText(psCmd, "Copied PowerShell validation command to clipboard!");
    });
  });

  // Direct download buttons
  const downloadBtns = document.querySelectorAll(".btn-trigger-download");
  downloadBtns.forEach(btn => {
    btn.addEventListener("click", (e) => {
      const href = btn.getAttribute("href") || "";
      if (href.startsWith("#")) {
        return; // Allow router to switch views smoothly
      }
      e.preventDefault();
      triggerDownload();
    });
  });
});
