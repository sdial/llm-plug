tailwind.config = {
    theme: {
        extend: {
            colors: {
                brand: {
                    50: '#f0efff',
                    100: '#e0dfff',
                    200: '#c7c4ff',
                    300: '#adabff',
                    400: '#8a86ff',
                    500: '#635bff',
                    600: '#554ce6',
                    700: '#4a42cc',
                    800: '#3730a8',
                    900: '#2c2785',
                },
                surface: {
                    50: '#faf9f7',
                    100: '#f5f5f4',
                    200: '#e8e6e3',
                    300: '#d4d1cc',
                    400: '#bcb8b1',
                    500: '#a09c94',
                },
                ink: {
                    50: '#f5f5f5',
                    100: '#e8e8e8',
                    200: '#cfcfcf',
                    300: '#b3b3b3',
                    400: '#9ca3af',
                    500: '#808080',
                    600: '#6b6b6b',
                    700: '#3f3f3f',
                    800: '#2d2d2d',
                    900: '#1a1a1a',
                }
            },
            fontFamily: {
                sans: ['-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'system-ui', 'sans-serif'],
                mono: ['SF Mono', 'Cascadia Code', 'Consolas', 'monospace'],
            },
            boxShadow: {
                'card': '0 1px 2px rgba(0,0,0,0.04), 0 1px 3px rgba(0,0,0,0.02)',
                'card-hover': '0 4px 8px rgba(0,0,0,0.06), 0 2px 4px rgba(0,0,0,0.03)',
                'modal': '0 20px 60px rgba(0,0,0,0.15), 0 8px 24px rgba(0,0,0,0.08)',
                'subtle': '0 0 0 1px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.04)',
            },
            borderRadius: {
                'card': '12px',
                'button': '8px',
                'input': '8px',
                'badge': '9999px',
            },
            animation: {
                'fade-in': 'fadeIn 0.2s ease-out',
                'slide-up': 'slideUp 0.3s cubic-bezier(0.16, 1, 0.3, 1)',
                'scale-in': 'scaleIn 0.2s ease-out',
            },
            keyframes: {
                fadeIn: {
                    '0%': { opacity: '0' },
                    '100%': { opacity: '1' },
                },
                slideUp: {
                    '0%': { opacity: '0', transform: 'translateY(8px)' },
                    '100%': { opacity: '1', transform: 'translateY(0)' },
                },
                scaleIn: {
                    '0%': { opacity: '0', transform: 'scale(0.95)' },
                    '100%': { opacity: '1', transform: 'scale(1)' },
                },
            }
        }
    }
}
