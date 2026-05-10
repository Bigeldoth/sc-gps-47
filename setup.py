"""SpaceDrive GPS — overlay de navigation pour Star Citizen.

Ce setup.py reste minimal : la distribution principale est l'exécutable
PyInstaller (voir spaceDrive.spec), pas un paquet pip publié.
"""
from setuptools import setup, find_packages

setup(
    name='spacedrive-gps',
    use_scm_version=True,
    setup_requires=['setuptools_scm'],
    description='Overlay GPS pour Star Citizen via OCR du HUD debug.',
    long_description=open('README.md', encoding='utf-8').read(),
    long_description_content_type='text/markdown',
    author='Bigeldoth',
    url='https://github.com/Bigeldoth/sc-gps-47',
    license='MIT',
    packages=find_packages(where='src'),
    package_dir={'': 'src'},
    python_requires='>=3.10',
    install_requires=[
        'mss>=9.0.0',
        'numpy>=1.24.0',
        'opencv-python>=4.8.0',
        'Pillow>=10.0.0',
        'pytesseract>=0.3.10',
        'PyQt6>=6.5.0',
        'pynput>=1.7.6',
    ],
    entry_points={
        'console_scripts': [
            'spacedrive=main:main',
        ],
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: End Users/Desktop',
        'License :: OSI Approved :: MIT License',
        'Operating System :: Microsoft :: Windows',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Topic :: Games/Entertainment',
    ],
)
