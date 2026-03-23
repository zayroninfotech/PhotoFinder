const PptxGenJS = require("pptxgenjs");

const pres = new PptxGenJS();

const colors = {
  primary: "1E2761",
  accent: "F59E0B",
  light: "F8FAFC",
  text: "1E293B"
};

// Slide 1: Title
let slide1 = pres.addSlide();
slide1.background = { color: colors.primary };
slide1.addText("PhotoFinder", {
  x: 0.5, y: 2, w: 9, h: 1,
  fontSize: 60, bold: true, color: "#FFFFFF", align: "center"
});
slide1.addText("Face Recognition Photo Matching System", {
  x: 0.5, y: 3.2, w: 9, h: 0.6,
  fontSize: 24, color: colors.accent, align: "center"
});

// Slide 2: System Architecture
let slide2 = pres.addSlide();
slide2.background = { color: colors.light };
slide2.addText("System Architecture", {
  x: 0.5, y: 0.3, w: 9, h: 0.5,
  fontSize: 32, bold: true, color: colors.primary
});

// Admin
slide2.addShape(pres.ShapeType.roundRect, {
  x: 0.5, y: 1.2, w: 2.8, h: 1.8,
  fill: { color: colors.primary }, line: { color: colors.accent, width: 2 }
});
slide2.addText("Admin Dashboard", {
  x: 0.5, y: 1.3, w: 2.8, h: 0.4,
  fontSize: 14, bold: true, color: "#FFFFFF", align: "center"
});
slide2.addText("• Submit Drive\n• Create Event\n• Download QR", {
  x: 0.6, y: 1.8, w: 2.6, h: 1,
  fontSize: 11, color: "#FFFFFF"
});

// Backend
slide2.addShape(pres.ShapeType.roundRect, {
  x: 3.7, y: 1.2, w: 2.8, h: 1.8,
  fill: { color: colors.accent }, line: { color: colors.primary, width: 2 }
});
slide2.addText("Backend Engine", {
  x: 3.7, y: 1.3, w: 2.8, h: 0.4,
  fontSize: 14, bold: true, color: "#FFFFFF", align: "center"
});
slide2.addText("• DeepFace\n• Auto-Sync\n• Face Encoding", {
  x: 3.8, y: 1.8, w: 2.6, h: 1,
  fontSize: 11, color: "#FFFFFF"
});

// User
slide2.addShape(pres.ShapeType.roundRect, {
  x: 6.9, y: 1.2, w: 2.8, h: 1.8,
  fill: { color: colors.primary }, line: { color: colors.accent, width: 2 }
});
slide2.addText("User Portal", {
  x: 6.9, y: 1.3, w: 2.8, h: 0.4,
  fontSize: 14, bold: true, color: "#FFFFFF", align: "center"
});
slide2.addText("• Scan QR\n• Upload Selfie\n• View Matches", {
  x: 7, y: 1.8, w: 2.6, h: 1,
  fontSize: 11, color: "#FFFFFF"
});

// Slide 3: Admin Flow
let slide3 = pres.addSlide();
slide3.background = { color: colors.light };
slide3.addText("Admin Workflow", {
  x: 0.5, y: 0.3, w: 9, h: 0.5,
  fontSize: 32, bold: true, color: colors.primary
});

const steps_admin = [
  { x: 0.5, y: 1.2, num: "1", title: "Submit Drive", desc: "Paste folder URL" },
  { x: 3, y: 1.2, num: "2", title: "Download", desc: "Get all photos" },
  { x: 5.5, y: 1.2, num: "3", title: "Encode", desc: "Create face vectors" },
  { x: 0.5, y: 2.5, num: "4", title: "Generate QR", desc: "Event code" },
  { x: 3, y: 2.5, num: "5", title: "Status Ready", desc: "51 indexed" },
  { x: 5.5, y: 2.5, num: "6", title: "Share", desc: "Send QR link" }
];

steps_admin.forEach(s => {
  slide3.addShape(pres.ShapeType.ellipse, {
    x: s.x, y: s.y, w: 0.4, h: 0.4,
    fill: { color: colors.accent }
  });
  slide3.addText(s.num, {
    x: s.x, y: s.y, w: 0.4, h: 0.4,
    fontSize: 16, bold: true, color: "#FFFFFF", align: "center", valign: "middle"
  });
  slide3.addText(s.title, {
    x: s.x + 0.5, y: s.y, w: 1.8, h: 0.25,
    fontSize: 11, bold: true, color: colors.primary
  });
  slide3.addText(s.desc, {
    x: s.x + 0.5, y: s.y + 0.25, w: 1.8, h: 0.35,
    fontSize: 9, color: colors.text
  });
});

// Slide 4: User Flow
let slide4 = pres.addSlide();
slide4.background = { color: colors.light };
slide4.addText("User Workflow", {
  x: 0.5, y: 0.3, w: 9, h: 0.5,
  fontSize: 32, bold: true, color: colors.primary
});

const steps_user = [
  { x: 0.5, y: 1.2, num: "1", title: "Scan QR", desc: "Open event" },
  { x: 3, y: 1.2, num: "2", title: "Upload", desc: "Face photo" },
  { x: 5.5, y: 1.2, num: "3", title: "Match", desc: "Compare faces" },
  { x: 0.5, y: 2.5, num: "4", title: "Results", desc: "Photo gallery" },
  { x: 3, y: 2.5, num: "5", title: "View", desc: "Check matches" },
  { x: 5.5, y: 2.5, num: "6", title: "Download", desc: "Save/Email ZIP" }
];

steps_user.forEach(s => {
  slide4.addShape(pres.ShapeType.ellipse, {
    x: s.x, y: s.y, w: 0.4, h: 0.4,
    fill: { color: colors.accent }
  });
  slide4.addText(s.num, {
    x: s.x, y: s.y, w: 0.4, h: 0.4,
    fontSize: 16, bold: true, color: "#FFFFFF", align: "center", valign: "middle"
  });
  slide4.addText(s.title, {
    x: s.x + 0.5, y: s.y, w: 1.8, h: 0.25,
    fontSize: 11, bold: true, color: colors.primary
  });
  slide4.addText(s.desc, {
    x: s.x + 0.5, y: s.y + 0.25, w: 1.8, h: 0.35,
    fontSize: 9, color: colors.text
  });
});

// Slide 5: Auto-Sync (REAL-TIME)
let slide5 = pres.addSlide();
slide5.background = { color: colors.light };
slide5.addText("Real-Time Auto-Sync & Updates ⚡", {
  x: 0.5, y: 0.3, w: 9, h: 0.5,
  fontSize: 32, bold: true, color: colors.primary
});

// Admin side
slide5.addShape(pres.ShapeType.roundRect, {
  x: 0.5, y: 1.1, w: 4.2, h: 2.8,
  fill: { color: "#E0E7FF" }, line: { color: colors.primary, width: 2 }
});
slide5.addText("Admin Dashboard", {
  x: 0.7, y: 1.3, w: 3.8, h: 0.35,
  fontSize: 13, bold: true, color: colors.primary
});
slide5.addText("Auto-Refresh: Every 10 Minutes", {
  x: 0.7, y: 1.75, w: 3.8, h: 0.3,
  fontSize: 11, bold: true, color: colors.accent
});
const admin_items = [
  "✓ Check Drive for new photos",
  "✓ Auto-load in background",
  "✓ Update photo count",
  "✓ Re-index automatically"
];
admin_items.forEach((item, i) => {
  slide5.addText(item, {
    x: 0.9, y: 2.2 + (i * 0.35), w: 3.6, h: 0.3,
    fontSize: 10, color: colors.text
  });
});

// User side
slide5.addShape(pres.ShapeType.roundRect, {
  x: 5.3, y: 1.1, w: 4.2, h: 2.8,
  fill: { color: "#DBEAFE" }, line: { color: colors.accent, width: 2 }
});
slide5.addText("User Event Page", {
  x: 5.5, y: 1.3, w: 3.8, h: 0.35,
  fontSize: 13, bold: true, color: colors.primary
});
slide5.addText("Live Updates: Every 5-10 Seconds", {
  x: 5.5, y: 1.75, w: 3.8, h: 0.3,
  fontSize: 11, bold: true, color: colors.accent
});
const user_items = [
  "✓ Poll for new matches",
  "✓ Auto-load results",
  "✓ Real-time gallery",
  "✓ No manual refresh"
];
user_items.forEach((item, i) => {
  slide5.addText(item, {
    x: 5.7, y: 2.2 + (i * 0.35), w: 3.6, h: 0.3,
    fontSize: 10, color: colors.text
  });
});

// Slide 6: Tech Stack
let slide6 = pres.addSlide();
slide6.background = { color: colors.light };
slide6.addText("Technology Stack", {
  x: 0.5, y: 0.3, w: 9, h: 0.5,
  fontSize: 32, bold: true, color: colors.primary
});

const tech_stack = [
  { cat: "Backend", tech: "Python Flask, Gunicorn" },
  { cat: "AI/ML", tech: "DeepFace, Facenet512, OpenCV" },
  { cat: "Storage", tech: "Google Drive, MongoDB" },
  { cat: "Frontend", tech: "HTML5, CSS3, JavaScript" },
  { cat: "QR Code", tech: "qrcode[pil]" },
  { cat: "Email", tech: "Gmail SMTP" }
];

tech_stack.forEach((s, i) => {
  const x = i < 3 ? 0.5 : 5.3;
  const y = 1.2 + ((i % 3) * 0.95);

  slide6.addShape(pres.ShapeType.roundRect, {
    x: x, y: y, w: 4.2, h: 0.75,
    fill: { color: "#F3F4F6" }, line: { color: colors.primary, width: 1 }
  });

  slide6.addText(s.cat, {
    x: x + 0.2, y: y + 0.08, w: 1.2, h: 0.3,
    fontSize: 11, bold: true, color: colors.primary
  });

  slide6.addText(s.tech, {
    x: x + 1.6, y: y + 0.08, w: 2.4, h: 0.3,
    fontSize: 10, color: colors.text
  });
});

// Slide 7: Key Features
let slide7 = pres.addSlide();
slide7.background = { color: colors.primary };
slide7.addText("Key Features", {
  x: 0.5, y: 0.4, w: 9, h: 0.5,
  fontSize: 32, bold: true, color: "#FFFFFF"
});

const features = [
  { icon: "⚡", title: "Fast", desc: "Face match < 2 sec" },
  { icon: "🔄", title: "Auto-Sync", desc: "Real-time updates" },
  { icon: "🌐", title: "Google Drive", desc: "Direct integration" },
  { icon: "📱", title: "Mobile", desc: "Responsive design" },
  { icon: "🔒", title: "Secure", desc: "Public folders only" },
  { icon: "📊", title: "Scalable", desc: "50+ photos easily" }
];

features.forEach((f, i) => {
  const row = Math.floor(i / 3);
  const col = i % 3;
  const x = 0.5 + (col * 3);
  const y = 1.2 + (row * 1.5);

  slide7.addShape(pres.ShapeType.roundRect, {
    x: x, y: y, w: 2.8, h: 1.2,
    fill: { color: colors.accent }
  });

  slide7.addText(f.icon, {
    x: x + 0.1, y: y + 0.15, w: 0.4, h: 0.4,
    fontSize: 24, align: "center"
  });

  slide7.addText(f.title, {
    x: x + 0.6, y: y + 0.15, w: 2, h: 0.3,
    fontSize: 11, bold: true, color: "#FFFFFF"
  });

  slide7.addText(f.desc, {
    x: x + 0.1, y: y + 0.65, w: 2.6, h: 0.4,
    fontSize: 9, color: "#FFFFFF"
  });
});

// Slide 8: Closing
let slide8 = pres.addSlide();
slide8.background = { color: colors.accent };
slide8.addText("Ready to Find Your Photos?", {
  x: 0.5, y: 2, w: 9, h: 1,
  fontSize: 48, bold: true, color: "#FFFFFF", align: "center"
});
slide8.addText("Scan • Upload • Match • Download", {
  x: 0.5, y: 3.2, w: 9, h: 0.6,
  fontSize: 20, color: colors.primary, align: "center"
});

pres.save({ path: "C:\\find_photos\\QR\\PhotoFinder_Flow_Diagram.pptx" });
console.log("✅ Presentation created!");
