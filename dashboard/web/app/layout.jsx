import './globals.css';

export const metadata = {
  title: 'honestapply — applications',
  description: 'One table with every application and all locally-stored artifacts.',
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
