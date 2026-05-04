from setuptools import setup, find_packages

setup(
    name='spacedrive-gps',
    use_scm_version=True,
    setup_requires=['setuptools_scm'],
    description='GPS Overlay for Star Citizen',
    author='Bigeldoth',
    author_email='contact@example.com',
    packages=find_packages(),
    install_requires=[
        'PyQt6',
        'keyboard',
        'pytesseract',
        'opencv-python',
    ],
    entry_points={
        'console_scripts': [
            'spacedrive=src.main:main',
        ],
    },
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: End Users/Desktop',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
    ],
)